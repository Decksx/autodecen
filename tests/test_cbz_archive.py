import io
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest
from PIL import Image

import api
from scripts import process_cbz


def _image_bytes(image_format, color):
    output = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(output, format=image_format)
    return output.getvalue()


def test_mislabeled_text_page_fails_before_model_processing(tmp_path):
    source = tmp_path / "Book.cbz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("001.jpg", _image_bytes("JPEG", (255, 0, 0)))
        archive.writestr("024.jpg", b"This gallery has been downloaded from HentaiFox.com")
    original = source.read_bytes()

    with pytest.raises(ValueError, match=r"Invalid image member '024.jpg' in CBZ .*UnidentifiedImageError"):
        api.extract_cbz_images(str(source), str(tmp_path / "stage"), set())

    assert source.read_bytes() == original
    assert not (tmp_path / "stage" / "024.jpg").exists()


def test_rebuild_preserves_paths_metadata_and_non_image_members(tmp_path, monkeypatch):
    source = tmp_path / "Series" / "Book.cbz"
    source.parent.mkdir()
    page_info = zipfile.ZipInfo("Chapter 01/pages/001.jpg", (2020, 2, 3, 4, 5, 6))
    page_info.comment = b"page comment"
    page_info.external_attr = 0o100644 << 16
    xml_info = zipfile.ZipInfo("metadata/ComicInfo.xml", (2019, 1, 2, 3, 4, 6))
    note_info = zipfile.ZipInfo("extras/source.txt", (2018, 1, 2, 3, 4, 6))

    with zipfile.ZipFile(source, "w") as archive:
        archive.comment = b"archive comment"
        archive.writestr(page_info, _image_bytes("JPEG", (255, 0, 0)))
        archive.writestr(xml_info, b"<ComicInfo><Series>Example</Series></ComicInfo>")
        archive.writestr(note_info, b"unchanged auxiliary data")

    stage = tmp_path / "stage"
    _, details = api.extract_cbz_images(str(source), str(stage), set())
    processed = tmp_path / "processed.png"
    processed.write_bytes(_image_bytes("PNG", (0, 255, 0)))
    output_root = tmp_path / "output"
    monkeypatch.setattr(api, "CAMELIA_OUTPUT", str(output_root))

    result = api.create_processed_cbz(
        [{"filename": "001.png", "processed_path": str(processed)}],
        {
            **details,
            "original_name": source.name,
            "relative_path": "Series/Volume 1/Book.cbz",
            "source_path": None,
        },
        "session-not-used-as-a-directory",
    )

    rebuilt = Path(result["path"])
    assert rebuilt == output_root / "archives" / "Series" / "Volume 1" / "Book.cbz"
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(rebuilt) as after:
        assert after.namelist() == before.namelist()
        assert after.comment == before.comment
        comic_info = ElementTree.fromstring(after.read("metadata/ComicInfo.xml"))
        assert comic_info.findtext("Series") == "Example"
        assert "uncensored" in comic_info.findtext("Tags", "").casefold()
        assert after.read("extras/source.txt") == before.read("extras/source.txt")
        assert after.getinfo("Chapter 01/pages/001.jpg").date_time == page_info.date_time
        assert after.getinfo("Chapter 01/pages/001.jpg").comment == page_info.comment
        with after.open("Chapter 01/pages/001.jpg") as page, Image.open(page) as image:
            assert image.getpixel((0, 0))[1] > image.getpixel((0, 0))[0]


def test_output_root_mirrors_directories_and_can_preserve_filename(tmp_path, monkeypatch):
    source = tmp_path / "incoming" / "Series" / "Book.cbz"
    source.parent.mkdir(parents=True)
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("001.png", _image_bytes("PNG", (255, 0, 0)))
    _, details = api.extract_cbz_images(str(source), str(tmp_path / "stage"), set())
    processed = tmp_path / "processed.png"
    processed.write_bytes(_image_bytes("PNG", (0, 255, 0)))
    destination = tmp_path / "destination"

    result = api.create_processed_cbz(
        [{"filename": "001.png", "processed_path": str(processed)}],
        {
            **details,
            "original_name": source.name,
            "relative_path": "Series/Book.cbz",
            "source_path": str(source),
            "output_root": str(destination),
            "preserve_archive_name": True,
        },
        "test-session",
    )

    assert Path(result["path"]) == destination / "Series" / "Book.cbz"
    assert not source.exists()
    assert result["original_deleted"] is True


def test_replace_mode_quarantines_original_before_installing_output(tmp_path):
    source = tmp_path / "Book.cbz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("001.png", _image_bytes("PNG", (255, 0, 0)))
        archive.writestr("ComicInfo.xml", b"<ComicInfo />")
    original_bytes = source.read_bytes()
    _, details = api.extract_cbz_images(str(source), str(tmp_path / "stage"), set())
    processed = tmp_path / "processed.png"
    processed.write_bytes(_image_bytes("PNG", (0, 255, 0)))

    result = api.create_processed_cbz(
        [{"filename": "001.png", "processed_path": str(processed)}],
        {
            **details,
            "original_name": source.name,
            "source_path": str(source),
            "output_mode": "replace",
            "backup_dir": str(tmp_path / "quarantine"),
        },
        "job-id",
    )

    assert Path(result["path"]) == source
    assert Path(result["backup_path"]).read_bytes() == original_bytes
    with zipfile.ZipFile(source) as archive, archive.open("001.png") as page:
        with Image.open(page) as image:
            assert image.getpixel((0, 0))[1] > image.getpixel((0, 0))[0]


def test_copy_mode_preserves_source_and_refuses_existing_destination(tmp_path):
    source = tmp_path / "incoming" / "Book.cbz"
    source.parent.mkdir()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("001.png", _image_bytes("PNG", (255, 0, 0)))
        archive.writestr("ComicInfo.xml", b"<ComicInfo><Tags>Color</Tags></ComicInfo>")
    original_bytes = source.read_bytes()
    _, details = api.extract_cbz_images(str(source), str(tmp_path / "stage"), set())
    processed = tmp_path / "processed.png"
    processed.write_bytes(_image_bytes("PNG", (0, 255, 0)))
    output_root = tmp_path / "Comix"
    context = {
        **details,
        "original_name": source.name,
        "source_path": str(source),
        "output_mode": "copy",
        "output_root": str(output_root),
    }

    result = api.create_processed_cbz(
        [{"filename": "001.png", "processed_path": str(processed)}],
        context,
        "copy-job",
    )

    assert Path(result["path"]) == output_root / source.name
    assert source.read_bytes() == original_bytes
    assert result["original_deleted"] is False
    assert result["backup_path"] is None
    with zipfile.ZipFile(result["path"]) as archive:
        assert b"uncensored" in archive.read("ComicInfo.xml").lower()

    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        api.create_processed_cbz(
            [{"filename": "001.png", "processed_path": str(processed)}],
            context,
            "copy-job-again",
        )


def test_copy_mode_does_not_replace_output_created_during_processing(tmp_path, monkeypatch):
    source = tmp_path / "input" / "Book.cbz"
    source.parent.mkdir()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("001.png", _image_bytes("PNG", (255, 0, 0)))
    original = source.read_bytes()
    _, details = api.extract_cbz_images(str(source), str(tmp_path / "stage"), set())
    processed = tmp_path / "processed.png"
    processed.write_bytes(_image_bytes("PNG", (0, 255, 0)))
    output_root = tmp_path / "Comix"
    context = {
        **details, "original_name": source.name, "source_path": str(source),
        "output_mode": "copy", "output_root": str(output_root),
    }
    destination = output_root / source.name
    real_link = api.os.link

    def competing_install(temporary, final):
        destination.write_bytes(b"competing archive")
        return real_link(temporary, final)

    monkeypatch.setattr(api.os, "link", competing_install)
    with pytest.raises(FileExistsError):
        api.create_processed_cbz(
            [{"filename": "001.png", "processed_path": str(processed)}],
            context, "copy-race",
        )

    assert destination.read_bytes() == b"competing archive"
    assert source.read_bytes() == original


def test_process_cbz_cli_accepts_repeated_ordered_model_stages():
    args = process_cbz.build_parser().parse_args([
        "Book.cbz",
        "--model-type", "transparent_black",
        "--model-type", "black_bars",
        "--destination-dir", r"X:\Library\Comix",
    ])

    assert args.model_types == ["transparent_black", "black_bars"]
    assert args.destination_dir == Path(r"X:\Library\Comix")


def test_process_cbz_cli_keeps_black_bars_as_the_implicit_default():
    args = process_cbz.build_parser().parse_args(["Book.cbz"])

    assert args.model_types is None


def test_process_cbz_cli_accepts_mosaic_stage():
    args = process_cbz.build_parser().parse_args(["Book.cbz", "--model-type", "mosaic"])

    assert args.model_types == ["mosaic"]


def test_process_cbz_cli_accepts_non_destructive_copy_output():
    args = process_cbz.build_parser().parse_args([
        "Book.cbz", "--copy", "--output-dir", r"X:\comix\processed",
        "--model-type", "mosaic",
    ])

    assert args.copy is True
    assert args.output_dir == Path(r"X:\comix\processed")
    assert args.model_types == ["mosaic"]
