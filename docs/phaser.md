# US note phaser

Set the effect on an enabled note from the audio thread, after `note_enable`:

```c
note_set_phaser(note, 64); // Sweep position, clamped to 0..127.
note_set_phaser(note, 0);  // Fade to unmodified dry audio, then stop processing.
```

`note->phaser` is the requested value and can also be assigned directly. There
is no automatic LFO: sequence/game automation supplies the sweep. Note reuse
resets the value to zero. This does not add a sequence opcode or a cross-thread
command. The implementation is US-only and uses the original audio microcode.

## Tone and range

The default is four first-order all-pass stages with **inverted wet polarity**
(`NOTE_PHASER_INVERT_WET 1`). This produces a more hollow, wah-like sound: two
moving pass bands, a notch between them, and attenuation at the low and high
ends of the spectrum. The stronger bass cut is intentional. Peaks retain
approximately unity gain; this changes the timbre rather than adding volume.
There is no resonant feedback.

The sweep is logarithmic in the first pass-band frequency, so equal value
increments move through equal frequency ratios. This allocates more control
positions to the low and middle frequencies than a linear coefficient sweep.
Signed coefficients extend the top end beyond the old positive-pole limit.
At the reference 32 kHz sample rate, the default four-stage response is:

| Value | First pass-band peak | Interior notch | Second pass-band peak |
|---|---:|---:|---:|
| 1 | 50 Hz | 121 Hz | 292 Hz |
| 64 | 632 Hz | 1517 Hz | 3541 Hz |
| 127 | 8000 Hz | 12000 Hz | 14269 Hz |

Set `NOTE_PHASER_INVERT_WET` to `0` in `include/config/config_audio.h` for the
classic sound. That swaps the pass bands and cancellations: the two peak
columns become the moving notches, with less bass attenuation. In two-stage
mode, inverted polarity produces a single moving band-pass peak (roughly
121–12000 Hz), while classic polarity produces a moving notch in that range.
All frequencies scale with the actual audio sample rate.

Both polarities use equal dry/wet magnitudes for maximum cancellation.
Increasing the wet proportion above 50% would make the cancellations shallower;
100% all-pass output would remove the static magnitude notches entirely.
Resonant feedback would be a further way to emphasize the sound, but is not
included because it would require additional DSP work.

## Processing

Filtering runs once per synthesis update, typically 128–144 samples, even while
the value changes. Each stage uses a normalized one-pole state:

```
u[n] = (1-|a|)*x[n] + a*u[n-1]
y[n] = (1+|a|)*u[n-1] - a*x[n]
```

This produces the same first-order all-pass transfer function, while eliminating
separate input histories and their save commands. Four stages need only the
same 32 bytes of filter history that the old two-stage implementation used.
The CPU only builds commands and advances controls; all PCM filtering and
mixing stay on the RSP. The coefficient comes directly from the selected book,
so regenerating the sweep cannot leave a separate CPU formula stale.

Bypass fades use the stock `ENVMIXER` volume interpolation, independently of
filtering. Every fade supplies its starting volume with `A_INIT`, so all notes
share one 80-byte envelope save area. The CPU never reads RSP-written history.
A pair of half-volume envelope outputs avoids the stock exponential envelope's
signed accumulator overflow near unity volume.

## Cost

Additional commands per note per synthesis update, excluding normal synthesis:

| Implementation | Steady sweep or settled value | Maximum, including activation/fade | Extra command-buffer RAM* |
|---|---:|---:|---:|
| Previous two-stage version | Up to 290 sweeping; 29 settled | 292 | 730 KiB |
| New four-stage default | 39 | 51 | 127.5 KiB |
| New two-stage option | 23 | 33 | 82.5 KiB |

\* With the configured maximum of 40 notes, four updates per frame, and two
command buffers. The default saves **82.5%** of the previous command reservation.
The immutable filter books also shrink from 8096 to 4064 bytes. Filter histories
use 32 bytes per note in four-stage mode, or 16 in two-stage mode; the envelope
save area is 80 bytes total. Existing six-byte note controls remain unchanged.
Fully bypassed notes issue no effect commands or state DMA.

The logarithmic sweep, signed-pole extension, and inverted blend add no RSP
commands, state buffers, or table entries to the simplified implementation.
Runtime trigonometry is unnecessary: the generator computes the sweep offline.
CPU coefficient selection adds only a table read and an absolute-value operation.

For the lowest cost, set `NOTE_PHASER_STAGES` to `2` in
`include/config/config_audio.h`. This uses one moving band in inverted mode
(one notch in classic mode) and keeps the smooth bypass fade. Stage count adjusts the
history allocation and command-buffer reservation automatically; supported
values are 2 and 4. Rebuild after changing it.

Command counts are **not cycle benchmarks**. Four-stage mode does more sample
filtering than the old settled two-stage path, but avoids repeatedly restarting
filters and transferring state during automation. Use two-stage mode when voice
count matters more than the richer four-stage response. Real-console RSP timing remains
unmeasured; no guarantee is made that every console voice can use the effect
simultaneously without audio underruns.

## Smoothing tradeoffs

Coefficient changes occur once per synthesis update. Sweep slew is slower: an
end-to-end jump takes roughly 0.25–0.32 seconds at 32 kHz with ordinary update
sizes. This limits zipper noise but smooths away very fast modulation. There
is still no sample-accurate coefficient interpolation.

Enabling or disabling the effect during an already-playing note uses a wet/dry
fade of approximately 32 ms. Zero is fully disabled after this release fade,
then dry output is bit-exact. Reversing a fade retains filter history.

When a new note starts with a nonzero phaser value, it begins at full effect
depth and the requested sweep position from its first sample. It skips the
wet fade; the normal note volume envelope handles the attack. Filter history
is still initialized in RSP command order, preventing reuse of a previous
note's state. Set the phaser value after `note_enable` and before the first
synthesis update to get this behavior. The filter's natural startup transient
remains, but there is no extra dry-to-wet delay at note onset.

The wet path retains quarter-scale internal headroom, but extreme full-scale
signals can still clip during filter transients. Listening tests on the target
emulator and hardware are still needed to judge residual switching artifacts.

`POLEF` mutates the DMEM coefficient table, so each stage reloads its book and
synthesis invalidates the ADPCM book cache afterward. Explicit state clearing
also avoids the original `POLEF` initialization quirk. Command behavior can be
checked against `rsp/audio.s` and the
[Mupen64Plus HLE implementation](https://github.com/mupen64plus/mupen64plus-rsp-hle/blob/master/src/alist.c).

## Validation

```sh
python3 tests/audio/test_phaser.py
make -j4 VERSION=us FIXLIGHTS=0
```

The host tests compile the production command builder in both stage modes and
interpret its emitted commands. They check impulse response, notch locations,
block-boundary histories, bypass, rapid automation, fade reversal, note reuse
and isolation, DMEM bounds, command budgets, and envelope accumulator limits.
This is not a full RSP emulator or a hardware timing test. Regenerate the
constant books with `python3 tools/gen_phaser_tables.py`. Both stage counts and
both polarities are tested, including negative poles, pass-band level, fully
processed note attacks, and retained fades for mid-note activation.
