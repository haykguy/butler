from butler import prompt_review
import pytest


def test_preview_is_read_only_and_preserves_existing_text(tmp_path):
    soul = tmp_path / 'SOUL.md'
    soul.write_text('My existing identity.\n')
    before = list(tmp_path.rglob('*'))
    result = prompt_review.review(tmp_path)
    assert list(tmp_path.rglob('*')) == before
    items = {item['id']: item for item in result['items']}
    assert set(items) == {'soul', 'capabilities'}
    assert items['soul']['current'] == 'My existing identity.\n'
    assert items['soul']['proposed'].startswith(items['soul']['current'])
    assert 'discover' in items['capabilities']['proposed'].lower()
    assert all(set(item) == {'id', 'path', 'current', 'proposed', 'diff', 'sha256', 'description'} for item in items.values())


def select(item):
    return {key: item[key] for key in ('id', 'sha256', 'proposed')}


def test_apply_selected_with_backup_and_idempotent_review(tmp_path):
    soul = tmp_path / 'SOUL.md'
    soul.write_text('original\n')
    item = prompt_review.review(tmp_path)['items'][0]
    result = prompt_review.apply(tmp_path, [select(item)])
    assert result['applied'] == ['soul']
    assert soul.read_text() == item['proposed']
    assert any(p.read_text() == 'original\n' for p in tmp_path.glob('SOUL.md.bak.*'))
    assert 'soul' not in {i['id'] for i in prompt_review.review(tmp_path)['items']}
    assert not (tmp_path / 'skills').exists()


def test_conflict_and_forged_proposal_do_not_write(tmp_path):
    soul = tmp_path / 'SOUL.md'
    soul.write_text('original')
    item = prompt_review.review(tmp_path)['items'][0]
    soul.write_text('changed')
    with pytest.raises(ValueError, match='conflict'):
        prompt_review.apply(tmp_path, [select(item)])
    assert soul.read_text() == 'changed'
    item = prompt_review.review(tmp_path)['items'][0]
    item['proposed'] = 'replace everything'
    with pytest.raises(ValueError):
        prompt_review.apply(tmp_path, [select(item)])
    assert soul.read_text() == 'changed'


def test_symlinks_and_missing_file_conflicts_are_rejected(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    home = tmp_path / 'profile'
    home.mkdir()
    item = prompt_review.review(home)['items'][0]
    (home / 'SOUL.md').write_text('')
    with pytest.raises(ValueError, match='conflict'):
        prompt_review.apply(home, [select(item)])
    (home / 'SOUL.md').unlink()
    (home / 'SOUL.md').symlink_to(outside / 'SOUL.md')
    with pytest.raises(ValueError):
        prompt_review.apply(home, [select(item)])
    assert not (outside / 'SOUL.md').exists()


def test_skills_symlink_is_never_followed(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (home / 'skills').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        prompt_review.review(home)
    assert not list(outside.iterdir())


def test_capability_template_is_a_generic_discovery_skill(tmp_path):
    item = next(i for i in prompt_review.review(tmp_path)['items'] if i['id'] == 'capabilities')
    text = item['proposed'].lower()
    assert text.startswith('---\nname:')
    for topic in ('environment', 'user surfaces', 'capabilities', 'skills', 'delegation', 'services', 'unknown', 'verif', 'inventory'):
        assert topic in text
    for private in ('desklinux', 'haykguy', '192.168.', 'mecha home'):
        assert private not in text


def test_failure_in_second_write_restores_first(tmp_path, monkeypatch):
    soul = tmp_path / 'SOUL.md'
    soul.write_text('untouched\n')
    selections = [select(i) for i in prompt_review.review(tmp_path)['items']]
    real_write = prompt_review._write_new

    def fail_second(fd, name, data, mode=0o600):
        if name.startswith('.SKILL.md.'):
            raise OSError('simulated write failure')
        return real_write(fd, name, data, mode)

    monkeypatch.setattr(prompt_review, '_write_new', fail_second)
    with pytest.raises(OSError, match='simulated'):
        prompt_review.apply(tmp_path, selections)
    assert soul.read_text() == 'untouched\n'
    assert not (tmp_path / 'skills/butler-capabilities/SKILL.md').exists()


def test_apply_user_edited_proposal_preserving_existing_text(tmp_path):
    soul = tmp_path / "SOUL.md"
    soul.write_text("Original identity.\n")
    item = prompt_review.review(tmp_path)["items"][0]
    item["proposed"] = "Original identity.\n\nMy own approved instructions.\n"
    prompt_review.apply(tmp_path, [select(item)])
    assert soul.read_text() == item["proposed"]


def test_inventory_discovers_safe_skill_metadata_without_secrets(tmp_path):
    skills = tmp_path / "skills"
    good = skills / "productivity" / "notes"
    good.mkdir(parents=True)
    (good / "SKILL.md").write_text("---\nname: notes\ndescription: Manage local notes safely.\n---\nSECRET_BODY_MUST_NOT_APPEAR\n")
    bad = skills / "private"
    bad.mkdir()
    (bad / "SKILL.md").write_text("---\nname: private\ndescription: token sk-secret-value\n---\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: escaped\ndescription: Outside skill.\n---\n")
    (skills / "escape").symlink_to(outside, target_is_directory=True)
    link = tmp_path.parent / (tmp_path.name + "-home-link")
    link.symlink_to(tmp_path, target_is_directory=True)
    item = next(i for i in prompt_review.review(link)["items"] if i["id"] == "capabilities")
    text = item["proposed"]
    assert "notes" in text and "Manage local notes safely." in text
    assert "SECRET_BODY" not in text and "sk-secret" not in text and "escaped" not in text
    assert "installed" in text.lower() and "not" in text.lower()


def test_preview_cannot_be_applied_to_another_profile(tmp_path):
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    item = prompt_review.review(first)['items'][0]
    with pytest.raises(ValueError, match='conflict'):
        prompt_review.apply(second, [select(item)])
    assert not (second / 'SOUL.md').exists()


def test_capabilities_review_is_idempotent_after_apply(tmp_path):
    item = next(i for i in prompt_review.review(tmp_path)['items'] if i['id'] == 'capabilities')
    prompt_review.apply(tmp_path, [select(item)])
    assert 'capabilities' not in {i['id'] for i in prompt_review.review(tmp_path)['items']}
