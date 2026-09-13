"""Direct tests for downloader.cleanup -- no FastAPI app or monkeypatching
needed, since these are now plain functions taking their inputs explicitly."""

from downloader.cleanup import cleanup_orphaned_contact_sheets, cleanup_orphaned_temp_files


def test_cleanup_orphaned_temp_files_leaves_active_usernames_alone(tmp_path):
    orphan_video = tmp_path / "alice_2026-04-27_10-00-00_video.mp4"
    orphan_audio = tmp_path / "alice_2026-04-27_10-00-00_audio.mp4"
    owned_video = tmp_path / "bob_2026-04-27_10-00-00_video.mp4"
    completed = tmp_path / "carol_2026-04-27_10-00-00.mp4"
    for f in (orphan_video, orphan_audio, owned_video, completed):
        f.write_bytes(b"data")

    removed = cleanup_orphaned_temp_files(tmp_path, active_usernames={"bob"})

    assert set(removed) == {orphan_video.name, orphan_audio.name}
    assert not orphan_video.exists()
    assert not orphan_audio.exists()
    assert owned_video.exists()
    assert completed.exists()


def test_cleanup_orphaned_temp_files_is_a_noop_on_empty_directory(tmp_path):
    assert cleanup_orphaned_temp_files(tmp_path, active_usernames=set()) == []


def test_cleanup_orphaned_contact_sheets_keeps_ones_with_a_source(tmp_path):
    orphan_sheet = tmp_path / "alice_2026-04-27_10-00-00_contactsheet.jpg"
    orphan_sheet.write_bytes(b"jpeg")
    kept_video = tmp_path / "bob_2026-04-27_11-00-00.mp4"
    kept_video.write_bytes(b"done")
    kept_sheet = tmp_path / "bob_2026-04-27_11-00-00_contactsheet.jpg"
    kept_sheet.write_bytes(b"jpeg")
    unrelated_jpg = tmp_path / "not_a_contact_sheet.jpg"
    unrelated_jpg.write_bytes(b"jpeg")

    removed = cleanup_orphaned_contact_sheets(tmp_path)

    assert removed == [orphan_sheet.name]
    assert not orphan_sheet.exists()
    assert kept_sheet.exists()
    assert unrelated_jpg.exists()
