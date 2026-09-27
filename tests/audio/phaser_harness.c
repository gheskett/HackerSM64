/* Host command interpreter for the production command builder.
 * The Python driver inserts the function and tables at PHASER_IMPLEMENTATION.
 * Emulates the stock RSP's rounding/alignment, including MIXER's 32-byte loop.
 */
#include <assert.h>
#include <stdint.h>
#include <string.h>
#include <math.h>

typedef int16_t s16;
typedef int32_t s32;
typedef float f32;
typedef struct { int op; uintptr_t a, b, c, d; } u64;
#define MIN(a,b) ((a) < (b) ? (a) : (b))
#define MAX(a,b) ((a) > (b) ? (a) : (b))
#define ALIGN16(a) (((a) + 15) & ~15)
#define TRUE 1
#define FALSE 0
#define A_INIT 1
#define A_CONTINUE 0
#define A_AUX 8
#define A_VOL 4
#define A_LEFT 2
#define A_RIGHT 0
#define A_RATE 0
#define DMEM_ADDR_TEMP 0
#define VIRTUAL_TO_PHYSICAL2(p) ((uintptr_t)(p))
#define VIRTUAL_TO_PHYSICAL(p) ((uintptr_t)(p))
struct NoteSynthesisBuffers { s16 phaserState[NOTE_PHASER_STAGES][4]; };
struct Note {
    unsigned char phaser, phaserActive;
    uint16_t phaserIndex, phaserWet;
    struct NoteSynthesisBuffers *synthesisBuffers;
};
enum { CLEAR, SET, MIX, LOAD, MOVE, SAVE, BOOK, POLE, VOLUME, RATE, ENVELOPE };
#define CMD(p,o,a,b,c,d) (*(p) = (u64){o,(uintptr_t)(a),(uintptr_t)(b),(uintptr_t)(c),(uintptr_t)(d)})
#define aClearBuffer(p,a,b) CMD(p,CLEAR,a,b,0,0)
#define aSetBuffer(p,f,a,b,c) CMD(p,SET,a,b,c,f)
#define aMix(p,f,a,b,c) CMD(p,MIX,a,b,c,0)
#define aLoadBuffer(p,a) CMD(p,LOAD,a,0,0,0)
#define aDMEMMove(p,a,b,c) CMD(p,MOVE,a,b,c,0)
#define aSaveBuffer(p,a) CMD(p,SAVE,a,0,0,0)
#define aLoadADPCM(p,a,b) CMD(p,BOOK,a,b,0,0)
#define aPoleFilter(p,a,b,c) CMD(p,POLE,a,b,c,0)
#define aSetVolume(p,f,a,b,c) CMD(p,VOLUME,f,a,b,c)
#define aSetVolume32(p,f,a,b) CMD(p,RATE,f,a,b,0)
#define aEnvMixer(p,f,a) CMD(p,ENVELOPE,f,a,0,0)

/* PHASER_IMPLEMENTATION */

static struct Note notes[2];
static struct NoteSynthesisBuffers states[2];
static u64 commands[1024];
static s16 dmem[2048], book[16];

static s16 clamp(int64_t x) { return MIN(32767, MAX(-32768, x)); }
static void execute(int n) {
    int in = 0, out = 0, count = 0;
    int right = 0, dry = 0, volumes[2] = {0}, targets[2] = {0}, rates[2] = {0};
    for (int k = 0; k < n; ++k) {
        u64 c = commands[k];
        int a = c.a, b = c.b, d = c.c;
        switch (c.op) {
        case CLEAR: memset((char *)dmem + a, 0, ALIGN16(b)); break;
        case SET:
            if (c.d & A_AUX) right = a;
            else { in = a; out = b; count = d; }
            break;
        case LOAD: memcpy((char *)dmem + (in & ~3), (void *)c.a, (count + 7) & ~7); break;
        case SAVE: memcpy((void *)c.a, (char *)dmem + (out & ~3), (count + 7) & ~7); break;
        case MOVE: memmove((char *)dmem + b, (char *)dmem + a, ALIGN16(d)); break;
        case BOOK: memcpy(book, (void *)c.b, a); break;
        case MIX:
            for (int i = 0; i < ((count + 31) & ~31) / 2; ++i) {
                int64_t sum = (int64_t)dmem[d/2+i]*32767 + (int64_t)dmem[b/2+i]*(s16)a;
                dmem[d/2+i] = clamp((sum + 16384) >> 15);
            }
            break;
        case POLE: {
            s16 *state = (s16 *)c.c;
            s16 previous = (a & A_INIT) ? 0 : state[3];
            s16 original[8];
            memcpy(original, book + 8, sizeof(original));
            for (int i = 0; i < 8; ++i) book[8+i] = (book[8+i] * ((b << 2) & 65535)) >> 16;
            for (int i = 0; i < count/2; i += 8) {
                for (int j = 0; j < 8; ++j) {
                    int64_t sum = (int64_t)dmem[in/2+i+j]*b + (int64_t)original[j]*previous;
                    for (int z = 0; z < j; ++z) sum += (int64_t)book[8+j-z-1]*dmem[in/2+i+z];
                    dmem[out/2+i+j] = clamp(sum >> 14);
                }
                previous = dmem[out/2+i+7];
            }
            memcpy(state, dmem + (out + count)/2 - 4, 8);
            break;
        }
        case VOLUME:
            if (a & A_AUX) dry = b;
            else volumes[(a & A_LEFT) ? 0 : 1] = b;
            break;
        case RATE:
            targets[(a & A_LEFT) ? 0 : 1] = b;
            rates[(a & A_LEFT) ? 0 : 1] = d;
            break;
        case ENVELOPE:
            assert(a == A_INIT); // Phaser explicitly supplies each ramp's initial volume.
            for (int channel = 0; channel < 2; ++channel) {
                int dst = (channel ? right : out) / 2;
                int64_t value = (int64_t)volumes[channel] * 65536;
                int64_t target = (int64_t)targets[channel] * 65536;
                int64_t sequence = (int64_t)volumes[channel] * rates[channel];
                int64_t step = target - value;
                for (int i = 0; i < count/2; i += 8) {
                    if (step) {
                        sequence = (sequence * rates[channel]) >> 16;
                        // Original US ENVMIXER stores its exponential sequence in s32.
                        assert(sequence >= INT32_MIN && sequence <= INT32_MAX);
                        step = (sequence - value) >> 3;
                    }
                    for (int j = 0; j < 8; ++j) {
                        value += step;
                        if ((step <= 0 && value <= target) || (step > 0 && value >= target)) {
                            value = target;
                            step = 0;
                        }
                        int gain = clamp((((value >> 16) * dry) + 16384) >> 15);
                        dmem[dst+i+j] = clamp(dmem[dst+i+j] + ((dmem[in/2+i+j] * gain) >> 15));
                    }
                }
                assert(value == target); // No unknown wet gain carried into the next update.
            }
            memset((void *)c.b, 0, 80);
            break;
        }
    }
    // Effects must never overwrite dry/wet stereo accumulators.
    for (int i = 0x4c0/2; i < 0x9c0/2; ++i) assert(dmem[i] == 12345);
}

void reset(void) {
    memset(notes, 0, sizeof(notes));
    memset(states, 0x55, sizeof(states)); // Initialization must not depend on zeroed RDRAM.
    for (int i = 0; i < 2; ++i) notes[i].synthesisBuffers = &states[i];
}

int process(int id, int value, s16 *samples, int length, int init) {
    assert(length >= 8 && length <= 160 && length % 8 == 0);
    struct Note *note = &notes[id];
    note->phaser = value;
    memset(dmem, 0x31, sizeof(dmem));
    for (int i = 0x4c0/2; i < 0x9c0/2; ++i) dmem[i] = 12345;
    memcpy(dmem, samples, length * 2);
    int count = note_apply_phaser(commands, note, length * 2, init ? A_INIT : 0) - commands;
    assert(count <= 15 + 9 * NOTE_PHASER_STAGES);
    execute(count);
    memcpy(samples, dmem, length * 2);
    return count;
}

int wet(int id) { return notes[id].phaserWet; }
int index_of(int id) { return notes[id].phaserIndex; }
