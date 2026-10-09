from pathlib import Path


class LocalStorage:
    """Where certificate files live. The only place that knows about the filesystem layout.

    Swapping to S3 means implementing the same small surface (path for writing is the one
    local-specific detail; an S3 version would upload after rendering to a temp file).
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key_for(job_id: str, certificate_id: str) -> str:
        # Deterministic: regenerating a certificate always targets the same file (idempotent).
        return f"{job_id}/{certificate_id}.pdf"

    def write_path(self, key: str) -> Path:
        return self.root / key

    def existing_file(self, key: str) -> Path | None:
        """Resolve a stored key to a real file, refusing anything that escapes the storage root."""
        root = self.root.resolve()
        path = (root / key).resolve()
        if root not in path.parents or not path.is_file():
            return None
        return path

    def is_safe_key(self, key: str) -> bool:
        root = self.root.resolve()
        return root in (root / key).resolve().parents

    def check_writable(self) -> bool:
        probe = self.root / ".ready"
        try:
            probe.write_text("ok")
            probe.unlink()
            return True
        except OSError:
            return False
