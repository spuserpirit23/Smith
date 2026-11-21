"""FileRepo: device records survive a backend restart (local, single-machine runs)."""
import pytest

from mitra.api.provisioning import make_device
from mitra.repo import FileRepo, make_repo


@pytest.mark.asyncio
async def test_devices_survive_restart(tmp_path):
    path = tmp_path / "devices.json"
    repo = make_repo(f"file:{path}")
    assert isinstance(repo, FileRepo)
    await repo.init()
    dev, secret = make_device("mitra-001", "main", "", None, None, False)
    await repo.create_device(dev)
    await repo.update_device("mitra-001", fw_version="0.1.0")

    # The secret itself is never written, only its hash.
    assert secret not in path.read_text(encoding="utf-8")

    again = FileRepo(str(path))   # "restart"
    await again.init()
    d = await again.get_device("mitra-001")
    assert d is not None and d.secret_hash == dev.secret_hash and d.fw_version == "0.1.0"
    assert d.created_at == dev.created_at

    await again.delete_device("mitra-001")
    third = FileRepo(str(path))
    await third.init()
    assert await third.get_device("mitra-001") is None
