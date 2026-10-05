import copy
import json
import os
import unittest

from oversteer.proxy import device_library as lib


class DeviceProfileTest(unittest.TestCase):

    def test_shipped_profiles_load(self):
        profiles = lib.load_profiles()
        ids = {p['id'] for p in profiles}
        for want in ('logitech-g29-ps3', 'fanatec-clubsport-handbrake', 'thrustmaster-th8a',
                     'logitech-driving-force-shifter', 'thrustmaster-t500rs-shifter',
                     'thrustmaster-tss-handbrake-sparco', 'fanatec-clubsport-shifter'):
            self.assertIn(want, ids)

    def test_by_id(self):
        self.assertEqual([p['id'] for p in lib.by_id(0x046d, 0xc24f)], ['logitech-g29-ps3'])
        self.assertEqual([p['id'] for p in lib.by_id('0eb7', '00e5')], ['fanatec-clubsport-handbrake'])
        self.assertEqual(lib.by_id('dead', 'beef'), [])

    def test_identity_is_a_spec_identity(self):
        profile = lib.by_id('046d', 'c24f')[0]
        ident = lib.identity(profile)
        self.assertEqual((ident.vendor, ident.product, ident.version), (0x046d, 0xc24f, 0x0111))

    def test_unknown_class_by_default(self):
        for p in lib.load_profiles():
            self.assertEqual(p['anti_cheat_class'], 'unknown')

    def test_verified_needs_full_capture(self):
        for p in lib.load_profiles():
            if p['verified']:
                self.assertTrue(p['capture']['complete'])

    def _base(self):
        return copy.deepcopy(lib.by_id('046d', 'c24f')[0])

    def test_rejections(self):
        def bad(**change):
            data = self._base()
            data.update(change)
            with self.assertRaises(lib.ProfileError):
                lib.validate(data)
        bad(anti_cheat_class='maybe')
        bad(kind='toaster')
        bad(vendor='zzzz')
        bad(verified='yes')
        bad(verified=True)                       # incomplete capture
        bad(surprise=1)
        bad(capabilities={'keys': ['BTN_NOPE']})
        bad(capabilities={'abs': {'ABS_X': {'min': 5}}})
        bad(capabilities={'abs': {'ABS_X': {'min': 5, 'max': 1}}})
        bad(capabilities={'ff': ['FF_NOPE']})
        bad(phys='(')

    def test_load_rejects_mismatched_file_name(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'other.json'), 'w') as f:
                json.dump(self._base(), f)
            with self.assertRaises(lib.ProfileError):
                lib.load_profiles(d)


if __name__ == '__main__':
    unittest.main()
