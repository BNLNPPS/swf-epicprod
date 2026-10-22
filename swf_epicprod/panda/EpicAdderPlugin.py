"""Register ePIC output through PanDA without absent or failed files."""

from pandaserver.dataservice.adder_simple_plugin import AdderSimplePlugin


class EpicAdderPlugin(AdderSimplePlugin):
    """Apply the ATLAS file-status rule to the generic registration path.

    AdderGen marks optional files omitted from the pilot report as nooutput.
    In particular, an ePIC log held in S3 has no Rucio replica to register.
    Keep all file records on the job for AdderGen's subsequent processing.
    """

    def execute(self):
        files = self.job.Files
        self.job.Files = [
            file_spec for file_spec in files
            if file_spec.status not in ("nooutput", "failed")
        ]
        try:
            return super().execute()
        finally:
            self.job.Files = files
