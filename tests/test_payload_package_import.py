"""The payload modules the platform imports from the package import there,
as well as beside their siblings in the job: submission takes
validate_metadata from register_to_rucio, and a bare sibling import in it
stopped every submission on 2026-10-22 ("No module named 'tokens'")."""
import unittest


class PackageImportTest(unittest.TestCase):
    def test_submission_imports_validate_metadata(self):
        from swf_epicprod.payload.register_to_rucio import validate_metadata
        self.assertTrue(callable(validate_metadata))

    def test_register_to_rucio_takes_the_package_tokens(self):
        import swf_epicprod.payload.register_to_rucio as reg
        self.assertEqual(reg.tokens.__name__, 'swf_epicprod.payload.tokens')


if __name__ == '__main__':
    unittest.main()
