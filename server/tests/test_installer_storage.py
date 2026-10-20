from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_windows_installer_removes_each_downloaded_audio_archive():
    script = (ROOT / "installer/windows/install.ps1").read_text(encoding="utf-8")

    assert "$DownloadedArchive = $true" in script
    assert "if ($DownloadedArchive -and (Test-Path $Archive))" in script
    assert "Espaco insuficiente" in script


def test_windows_installer_accepts_python_installed_outside_portable_directory():
    script = (ROOT / "installer/windows/install.ps1").read_text(encoding="utf-8")

    assert "function Find-Python312" in script
    assert "Programs\\Python\\Python312\\python.exe" in script
    assert "PythonCore\\3.12\\InstallPath" in script
    assert "$PythonExe = Find-Python312" in script


def test_linux_installer_removes_each_downloaded_audio_archive():
    script = (ROOT / "installer/linux/install.sh").read_text(encoding="utf-8")

    assert "downloaded_archive = archive.parent == download_dir" in script
    assert "archive.unlink(missing_ok=True)" in script
    assert "Espaco insuficiente" in script
