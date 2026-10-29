import os
import sys
from io import BytesIO

from werkzeug.datastructures import FileStorage

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import file_manager as fm  # noqa: E402


def test_save_upload_tree_preserves_directory_structure(tmp_path):
    upload = FileStorage(stream=BytesIO(b"contenido"), filename="informe.txt")
    fm.save_upload_tree(str(tmp_path), "destino", "carpeta/docs/informe.txt", upload)
    assert (tmp_path / "destino" / "carpeta" / "docs" / "informe.txt").read_bytes() == b"contenido"


def test_resolve_path_allows_subpaths(tmp_path):
    (tmp_path / "sub").mkdir()
    resolved = fm.resolve_path(str(tmp_path), "sub")
    assert resolved == str(tmp_path / "sub")


def test_resolve_path_blocks_traversal(tmp_path):
    try:
        fm.resolve_path(str(tmp_path), "../../etc/passwd")
        assert False, "debería haber lanzado FileManagerError"
    except fm.FileManagerError:
        pass


def test_resolve_path_sandboxes_absolute_looking_paths(tmp_path):
    # Una ruta que empieza por "/" no debe interpretarse como absoluta real:
    # se trata como relativa dentro de la raíz permitida (sandbox), nunca
    # como una escapada hacia /etc/passwd del sistema real.
    resolved = fm.resolve_path(str(tmp_path), "/etc/passwd")
    assert resolved == str(tmp_path / "etc" / "passwd")
    assert resolved.startswith(str(tmp_path))


def test_list_directory_and_mkdir(tmp_path):
    fm.make_directory(str(tmp_path), "", "nueva_carpeta")
    listing = fm.list_directory(str(tmp_path))
    names = [e["name"] for e in listing["entries"]]
    assert "nueva_carpeta" in names


def test_mkdir_rejects_bad_names(tmp_path):
    try:
        fm.make_directory(str(tmp_path), "", "a/b")
        assert False
    except fm.FileManagerError:
        pass


def test_delete_path_file_and_dir(tmp_path):
    (tmp_path / "archivo.txt").write_text("hola")
    fm.delete_path(str(tmp_path), "archivo.txt")
    assert not (tmp_path / "archivo.txt").exists()

    (tmp_path / "carpeta").mkdir()
    (tmp_path / "carpeta" / "dentro.txt").write_text("x")
    fm.delete_path(str(tmp_path), "carpeta")
    assert not (tmp_path / "carpeta").exists()


def test_delete_root_is_blocked(tmp_path):
    try:
        fm.delete_path(str(tmp_path), "")
        assert False
    except fm.FileManagerError:
        pass


def test_zip_directory_to_buffer(tmp_path):
    (tmp_path / "carpeta").mkdir()
    (tmp_path / "carpeta" / "a.txt").write_text("contenido-a")
    (tmp_path / "carpeta" / "b.txt").write_text("contenido-b")

    buffer = fm.zip_directory_to_buffer(str(tmp_path), "carpeta")

    import zipfile
    with zipfile.ZipFile(buffer) as zf:
        names = set(zf.namelist())
        assert names == {"a.txt", "b.txt"}


def test_file_path_for_download_rejects_directories(tmp_path):
    (tmp_path / "carpeta").mkdir()
    try:
        fm.file_path_for_download(str(tmp_path), "carpeta")
        assert False
    except fm.FileManagerError:
        pass


def test_text_file_create_edit_and_read(tmp_path):
    fm.write_text_file(str(tmp_path), "nota.txt", "hola", create=True)
    assert fm.read_text_file(str(tmp_path), "nota.txt") == "hola"
    fm.write_text_file(str(tmp_path), "nota.txt", "adiós")
    assert (tmp_path / "nota.txt").read_text(encoding="utf-8") == "adiós"


def test_text_file_create_refuses_existing(tmp_path):
    (tmp_path / "nota.txt").write_text("x")
    try:
        fm.write_text_file(str(tmp_path), "nota.txt", "y", create=True)
        assert False, "debería haber lanzado FileManagerError"
    except fm.FileManagerError:
        pass
    assert (tmp_path / "nota.txt").read_text() == "x"


def test_read_text_file_rejects_binary(tmp_path):
    (tmp_path / "bin.dat").write_bytes(b"\xff\xfe\x00\x81")
    try:
        fm.read_text_file(str(tmp_path), "bin.dat")
        assert False, "debería haber lanzado FileManagerError"
    except fm.FileManagerError:
        pass
