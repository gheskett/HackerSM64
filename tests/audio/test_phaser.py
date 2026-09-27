#!/usr/bin/env python3
"""Run the actual C command builder against a host model of the US RSP commands.

Requires only Python's standard library and a host C compiler. Does not replace
listening tests or RSP/LLE timing measurements on the target platform.
"""
import ctypes
import importlib.util
import math
from pathlib import Path
import random
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class PhaserTests(unittest.TestCase):
    stages = 4
    inverted = 1

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        source = (ROOT / "src/audio/synthesis.c").read_text()
        implementation = source.split('#include "phaser_tables.inc.c"', 1)[1].split("\n#endif", 1)[0]
        tables = (ROOT / "src/audio/phaser_tables.inc.c").read_text()
        harness = (ROOT / "tests/audio/phaser_harness.c").read_text()
        cfile = Path(cls.temp.name) / "phaser.c"
        cfile.write_text(harness.replace("/* PHASER_IMPLEMENTATION */", tables + implementation))
        library = Path(cls.temp.name) / "phaser.so"
        subprocess.run(["cc", "-shared", "-fPIC", "-O2", "-Wall", "-Wextra", "-Werror",
                        f"-DNOTE_PHASER_STAGES={cls.stages}",
                        f"-DNOTE_PHASER_INVERT_WET={cls.inverted}",
                        "-fsanitize=undefined", "-fno-sanitize-recover=all", str(cfile), "-lm", "-o", str(library)], check=True)
        cls.lib = ctypes.CDLL(str(library))
        cls.lib.process.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.lib.reset()

    def process(self, samples, value, note=0, init=False):
        buffer = (ctypes.c_int16 * len(samples))(*samples)
        count = self.lib.process(note, value, buffer, len(samples), init)
        return list(buffer), count

    def settle(self, value, note=0):
        for _ in range(64):
            self.process([0] * 128, value, note)

    def coefficient(self, value):
        # Independent analytic reference: log-spaced first CLASSIC four-stage
        # notch, which becomes the first pass-band peak when wet is inverted.
        frequency = 50 * 160 ** ((value - 1) / 126)
        ratio = math.tan(math.pi * frequency / 32000) / math.tan(math.pi / 8)
        return max(-16000, min(16000, round(16384*(1-ratio)/(1+ratio)))) / 16384

    def test_generated_books(self):
        spec = importlib.util.spec_from_file_location("generator", ROOT / "tools/gen_phaser_tables.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.generate(), (ROOT / "src/audio/phaser_tables.inc.c").read_text())

    def test_bypass_is_bit_exact_and_free(self):
        rng = random.Random(1)
        for length in range(8, 161, 8):
            samples = [rng.randrange(-32768, 32768) for _ in range(length)]
            out, count = self.process(samples, 0, init=True)
            self.assertEqual(out, samples)
            self.assertEqual(count, 0)

    def test_note_attack_starts_with_full_effect(self):
        for value in (1, 64, 110, 127):
            for length in (8, 128, 144, 160):
                samples = [16000] + [0] * (length - 1)
                self.lib.reset()
                self.settle(value)
                reference, _ = self.process(samples, value)

                # A genuinely new note must have the same zero-history filter
                # response as an already-wet filter fed silence before attack.
                self.lib.reset()
                attack, count = self.process(samples, value, init=True)
                self.assertEqual(self.lib.wet(0), 16384)
                self.assertEqual(self.lib.index_of(0), value - 1)
                self.assertEqual(attack, reference, (value, length))
                self.assertEqual(count, 7 + 9*self.stages)

    def test_enabling_effect_during_note_still_fades(self):
        self.process([4000] * 128, 0, init=True)
        self.process([4000] * 128, 64)
        self.assertGreater(self.lib.wet(0), 0)
        self.assertLess(self.lib.wet(0), 16384)
        for _ in range(7):
            self.process([4000] * 128, 64)
        self.assertEqual(self.lib.wet(0), 16384)

    def test_impulse_matches_allpass_cascade(self):
        for value in (1, 32, 64, 96, 109, 110, 127):
            self.lib.reset()
            self.settle(value)
            result = []
            for block in range(8):
                samples = ([16000] + [0] * 127) if block == 0 else [0] * 128
                output, count = self.process(samples, value)
                self.assertEqual(count, 7 + 8*self.stages)
                result.extend(output)
            a = self.coefficient(value)
            xprev = [0.0] * self.stages
            yprev = [0.0] * self.stages
            expected = []
            for i in range(len(result)):
                dry = 16000.0 if i == 0 else 0.0
                x = dry
                for stage in range(self.stages):
                    y = xprev[stage] - a * x + a * yprev[stage]
                    xprev[stage], yprev[stage], x = x, y, y
                expected.append((dry + (-x if self.inverted else x)) / 2)
            error = max(abs(x-y) for x, y in zip(result, expected))
            self.assertLess(error, 80, (value, error))

    def test_notches_track_sweep(self):
        for value in (1, 64, 127):
            # Inverted two-stage mode has only endpoint nulls (a band-pass).
            # Inverted four-stage mode also has a moving interior null.
            notches = range(0, self.stages+1, 2) if self.inverted else range(1, self.stages, 2)
            for notch in notches:
                self.lib.reset()
                self.settle(value)
                a = self.coefficient(value)
                omega = 2 * math.atan((1-a)/(1+a) * math.tan(notch*math.pi/(2*self.stages)))
                output = []
                for block in range(48):
                    samples = [int(12000 * math.sin(omega * (block*128+i))) for i in range(128)]
                    if notch == 0:
                        samples = [12000] * 128
                    elif notch == self.stages:
                        samples = [12000, -12000] * 64
                    out, _ = self.process(samples, value)
                    if block > 8:
                        output.extend(out)
                rms = math.sqrt(sum(x*x for x in output) / len(output))
                self.assertLess(rms, 100, (value, notch, rms))

    def test_pass_bands_keep_their_level(self):
        for value in (1, 64, 127):
            bands = range(1, self.stages, 2) if self.inverted else range(0, self.stages+1, 2)
            for band in bands:
                self.lib.reset()
                self.settle(value)
                a = self.coefficient(value)
                omega = 2 * math.atan((1-a)/(1+a) * math.tan(band*math.pi/(2*self.stages)))
                output = []
                source = []
                for block in range(32):
                    samples = [int(12000 * math.sin(omega * (block*128+i))) for i in range(128)]
                    if band == 0:
                        samples = [12000] * 128
                    elif band == self.stages:
                        samples = [12000, -12000] * 64
                    out, _ = self.process(samples, value)
                    if block > 8:
                        output.extend(out)
                        source.extend(samples)
                gain = math.sqrt(sum(x*x for x in output) / sum(x*x for x in source))
                self.assertGreater(gain, 0.97, (value, band, gain))
                self.assertLess(gain, 1.03, (value, band, gain))

    def test_history_survives_update_boundaries(self):
        rng = random.Random(9)
        samples = [rng.randrange(-8000, 8001) for _ in range(1280)]
        results = []
        for chunk in (8, 16, 80, 128, 160):
            self.lib.reset()
            self.settle(71)
            output = []
            for start in range(0, len(samples), chunk):
                out, _ = self.process(samples[start:start+chunk], 71)
                output.extend(out)
            results.append(output)
        for result in results[1:]:
            self.assertEqual(result, results[0])

    def test_fade_reverse_reuse_and_note_isolation(self):
        self.settle(127)
        self.settle(1, note=1)
        self.process([4000] * 128, 0)
        self.assertGreater(self.lib.wet(0), 0)
        self.process([4000] * 128, 127)
        self.assertEqual(self.lib.index_of(0), 126)
        self.assertEqual(self.lib.index_of(1), 0)
        for _ in range(8):
            self.process([4000] * 128, 0)
        output, count = self.process([4000] * 128, 0)
        self.assertEqual((output, count), ([4000] * 128, 0))
        restarted, _ = self.process([4000] * 128, 64, init=True)
        self.lib.reset()
        fresh, _ = self.process([4000] * 128, 64, init=True)
        self.assertEqual(restarted, fresh)

    def test_automation_keeps_single_pass_command_cost(self):
        self.settle(1)
        for update in range(1, 11):
            _, count = self.process([0] * 128, 127)
            self.assertEqual(count, 7 + 8*self.stages)
            self.assertEqual(self.lib.index_of(0), 2*update)

    def test_rapid_automation_and_command_bound(self):
        rng = random.Random(8)
        peak_step = 0
        previous = 5000
        for block in range(2000):
            length = rng.choice((8, 128, 136, 144, 160))
            # DC isolates switching discontinuities from the source waveform.
            value = rng.choice((0, 1, 127, rng.randrange(128)))
            output, count = self.process([5000] * length, value)
            self.assertLessEqual(count, 15 + 9*self.stages)
            for sample in output:
                peak_step = max(peak_step, abs(sample-previous))
                previous = sample
        self.assertLess(peak_step, 400, peak_step)


class LightweightPhaserTests(PhaserTests):
    stages = 2


class ClassicPhaserTests(PhaserTests):
    inverted = 0


class ClassicLightweightPhaserTests(LightweightPhaserTests):
    inverted = 0


if __name__ == "__main__":
    unittest.main()
