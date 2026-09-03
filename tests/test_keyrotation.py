"""마스터 KEK 회전 — **유출 대응 중에 데이터를 잃지 않는다.**

이 파일이 지키는 단언은 하나로 줄일 수 있다: *회전은 어디서 중단돼도 원문을 되찾을
길을 남긴다.* 나머지는 그것의 각론이다.

`design-decisions.md` D4/G5 의 해제 조건이 "런북을 쓰는 시점" 이었고, 런북이 주장하는
절차가 실제로 그러한지를 여기서 잰다 — 유출 대응 절차는 **써 두기만 하면 반드시
어긋난다.** 그 절차를 쓸 일이 없기를 바라며 쓰는 문서이기 때문이다.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from app.crypto import CryptoError, KeyVault, generate_master_key, prompt_aad
from app.keyrotation import (
    KEY_NAME,
    RETIRED_PREFIX,
    RotationRefused,
    STAGED_NAME,
    interrupted,
    rotate_master_kek,
    staged_key_path,
    vault_from_file,
)
from app.store import SqliteStore
from tests.conftest import FakeClock


def vault_for(key: str) -> KeyVault:
    return KeyVault(base64.b64decode(key))


@pytest.fixture
def keys_dir(tmp_path) -> Path:
    directory = tmp_path / "keys"
    directory.mkdir()
    return directory


@pytest.fixture
def kek(keys_dir) -> str:
    key = generate_master_key()
    (keys_dir / KEY_NAME).write_text(key + "\n", encoding="utf-8")
    return key


@pytest.fixture
def store(tmp_path, kek) -> SqliteStore:
    store = SqliteStore(tmp_path / "rot.db", now=FakeClock())
    vault = vault_for(kek)
    for tenant in ("acme", "globex"):
        store.create_tenant(
            tenant, tenant.title(), locale="ko-KR", end_user_salt="s",
            dek_wrapped=vault.create_dek(),
        )
    yield store
    store.close()


def seal_a_prompt(store: SqliteStore, kek: str, tenant: str, text: str) -> tuple:
    """원문 하나를 봉인해 둔다 — 회전이 그것을 지키는지가 이 파일의 요지다."""
    vault = vault_for(kek)
    wrapped = store.wrapped_deks()[tenant]
    sealed = vault.seal(wrapped, text, aad=prompt_aad(tenant, "j1"))
    return sealed, prompt_aad(tenant, "j1")


# ── 회전이 하는 일 ──────────────────────────────────────────────────────────


def test_the_ciphertext_survives_the_rotation(store, keys_dir, kek):
    """**이 파일에서 가장 중요한 단언이다.** 회전 전에 봉인한 원문이 회전 뒤에 열린다."""
    sealed, aad = seal_a_prompt(store, kek, "acme", "분기 실적 요약")

    result = rotate_master_kek(store, keys_dir=keys_dir, old_vault=vault_for(kek))

    new_key = (keys_dir / KEY_NAME).read_text(encoding="utf-8").strip()
    assert new_key != kek
    reopened = vault_for(new_key).open(
        store.wrapped_deks()["acme"], sealed, aad=aad
    )
    assert reopened == "분기 실적 요약"
    assert result.tenants == 2


def test_the_old_key_stops_opening_anything(store, keys_dir, kek):
    """회전의 목적이 이것이다 — 유출된 키가 더는 아무것도 못 열어야 한다."""
    rotate_master_kek(store, keys_dir=keys_dir, old_vault=vault_for(kek))

    old = vault_for(kek)
    assert not any(old.can_open(w) for w in store.wrapped_deks().values())


def test_no_ciphertext_is_rewritten(store, keys_dir, kek):
    """**암호문은 건드리지 않는다.** 그것이 회전이 데이터 양과 무관하게 빠른 이유다.

    래핑만 바뀌었는지는 DEK 자체가 그대로인 것으로 확인한다 — DEK 가 바뀌면
    옛 암호문은 못 열고, 그 순간 회전은 crypto-shredding 이 된다.
    """
    before = vault_for(kek)._unwrap_bytes(store.wrapped_deks()["acme"])

    rotate_master_kek(store, keys_dir=keys_dir, old_vault=vault_for(kek))

    new_key = (keys_dir / KEY_NAME).read_text(encoding="utf-8").strip()
    after = vault_for(new_key)._unwrap_bytes(store.wrapped_deks()["acme"])
    assert after == before, "DEK 가 바뀌었다 — 회전이 아니라 파기다"


def test_the_old_key_is_kept_not_deleted(store, keys_dir, kek):
    """**확인 전에 지우면 되돌릴 길이 없다.** 언제 지울지는 런북이 말한다."""
    result = rotate_master_kek(store, keys_dir=keys_dir, old_vault=vault_for(kek))

    assert result.retired_key_path is not None
    assert result.retired_key_path.exists()
    assert result.retired_key_path.name.startswith(RETIRED_PREFIX)
    assert result.retired_key_path.read_text(encoding="utf-8").strip() == kek


def test_the_generated_key_is_returned_but_a_supplied_one_is_not(store, keys_dir, kek):
    """운영자가 준 키를 되찍지 않는다 — 남의 시크릿 매니저 값을 이쪽 로그에 흘리는 것이다."""
    supplied = generate_master_key()

    result = rotate_master_kek(
        store, keys_dir=keys_dir, old_vault=vault_for(kek), new_key=supplied
    )

    assert result.generated_key is None
    assert (keys_dir / KEY_NAME).read_text(encoding="utf-8").strip() == supplied


# ── 시작하면 안 되는 상태 ───────────────────────────────────────────────────


def test_rotation_refuses_when_a_tenant_is_already_unopenable(store, keys_dir, kek):
    """**회전은 고장을 고치는 도구가 아니다.**

    이미 못 여는 테넌트가 있는데 회전하면 그 테넌트는 영영 못 열게 되고, 운영자는
    회전이 그것을 깨뜨렸다고 믿는다 — 진짜 원인(키를 잘못 바꿨다)에서 더 멀어진다.
    """
    stranger = vault_for(generate_master_key())
    store._conn.execute(
        "UPDATE tenants SET dek_wrapped = ? WHERE id = 'globex'",
        (stranger.create_dek(),),
    )
    store._conn.commit()

    with pytest.raises(RotationRefused, match="globex"):
        rotate_master_kek(store, keys_dir=keys_dir, old_vault=vault_for(kek))


def test_nothing_changes_when_rotation_is_refused(store, keys_dir, kek):
    """거절은 **아무것도 안 바뀐 상태**에서만 난다."""
    before = dict(store.wrapped_deks())
    stranger = vault_for(generate_master_key())
    store._conn.execute(
        "UPDATE tenants SET dek_wrapped = ? WHERE id = 'globex'",
        (stranger.create_dek(),),
    )
    store._conn.commit()

    with pytest.raises(RotationRefused):
        rotate_master_kek(store, keys_dir=keys_dir, old_vault=vault_for(kek))

    assert store.wrapped_deks()["acme"] == before["acme"]
    assert not staged_key_path(keys_dir).exists(), "거절했는데 새 키를 남겼다"


def test_rotation_refuses_without_a_key(store, keys_dir):
    """원문 보관이 꺼진 설치처에는 회전할 키가 없다."""
    with pytest.raises(RotationRefused, match="마스터 KEK 가 없"):
        rotate_master_kek(store, keys_dir=keys_dir, old_vault=KeyVault(None))


def test_a_purged_tenant_does_not_block_the_rotation(store, keys_dir, kek):
    """파기된 테넌트의 DEK 는 NULL 이다. 그것을 "못 여는 테넌트" 로 세면 회전이
    영영 안 된다 — 파기는 정상 상태이지 사고가 아니다."""
    store._conn.execute(
        "UPDATE tenants SET dek_wrapped = NULL, purged_at = 1 WHERE id = 'globex'"
    )
    store._conn.commit()

    result = rotate_master_kek(store, keys_dir=keys_dir, old_vault=vault_for(kek))

    assert result.tenants == 1, "파기된 테넌트까지 세었다"


# ── 중단 ────────────────────────────────────────────────────────────────────


def test_an_interrupted_rotation_leaves_both_keys_on_disk(store, keys_dir, kek):
    """**없앨 수 없는 창을 두 키가 다 있는 창으로 만든다.**

    DB 커밋과 파일 교체 사이에서 죽으면 `master.key` 는 옛 키, DB 는 새 래핑이다.
    그 상태에서 복구할 수 있어야 하고, 복구에 필요한 것은 새 키다 — 그것이
    `master.key.new` 로 이미 디스크에 있다.
    """
    original = dict(store.wrapped_deks())

    def die(*args, **kwargs):
        raise KeyboardInterrupt("커밋 직후 죽는다")

    real_replace = store.replace_wrapped_deks

    def commit_then_die(rewrapped, *, actor):
        real_replace(rewrapped, actor=actor)
        die()

    store.replace_wrapped_deks = commit_then_die
    with pytest.raises(KeyboardInterrupt):
        rotate_master_kek(store, keys_dir=keys_dir, old_vault=vault_for(kek))

    staged = interrupted(keys_dir)
    assert staged is not None, "중단의 증거가 안 남았다"
    assert (keys_dir / KEY_NAME).read_text(encoding="utf-8").strip() == kek
    assert store.wrapped_deks() != original, "커밋됐어야 한다"

    # 런북이 말하는 복구: 무대에 올린 키를 제자리로 옮기면 전부 열린다.
    recovered = vault_for(staged.read_text(encoding="utf-8").strip())
    assert all(recovered.can_open(w) for w in store.wrapped_deks().values())


def test_a_second_rotation_refuses_while_one_is_interrupted(store, keys_dir, kek):
    """중단된 회전 위에 또 회전하면 세 키가 얽힌다. 먼저 정리하게 만든다."""
    staged_key_path(keys_dir).write_text(generate_master_key() + "\n", encoding="utf-8")

    with pytest.raises(RotationRefused, match="중단된 회전"):
        rotate_master_kek(store, keys_dir=keys_dir, old_vault=vault_for(kek))


def test_a_failure_while_rewrapping_leaves_no_evidence(store, keys_dir, kek):
    """DB 를 **안 건드렸음이 확실한** 구간에서만 무대의 키를 치운다.

    여기서 치우는 이유는 `doctor` 가 있지도 않은 중단을 보고하지 않게 하려는
    편의뿐이다.
    """
    class Exploding(KeyVault):
        def rewrap(self, wrapped_dek, new_vault):
            raise RuntimeError("래핑 중에 터진다")

    exploding = Exploding(base64.b64decode(kek))
    with pytest.raises(RuntimeError):
        rotate_master_kek(store, keys_dir=keys_dir, old_vault=exploding)

    assert interrupted(keys_dir) is None, "DB 를 안 건드렸는데 중단으로 보인다"


def test_a_failure_at_the_store_keeps_both_keys(store, keys_dir, kek):
    """**저장소를 부른 뒤부터는 무슨 일이 나든 두 키를 다 남긴다.**

    커밋됐는지 아닌지를 예외만 보고 판단할 수 없고, 틀린 쪽으로 판단해 새 키를
    지우면 **어떤 키로도 못 여는 DB** 가 남는다. 편의(깨끗한 진단)를 위해 그
    위험을 지지 않는다 — 어느 쪽이 사실인지는 `doctor` 가 실제로 열어 보고 판정한다.
    """
    def boom(rewrapped, *, actor):
        raise RuntimeError("커밋 도중 죽었다 — 됐는지 안 됐는지 모른다")

    store.replace_wrapped_deks = boom
    with pytest.raises(RuntimeError):
        rotate_master_kek(store, keys_dir=keys_dir, old_vault=vault_for(kek))

    assert interrupted(keys_dir) is not None, "복구에 필요한 새 키를 지웠다"


def test_the_staged_key_tells_which_side_the_db_is_on(store, keys_dir, kek):
    """**판정을 사람에게 떠넘기지 않는다.**

    "DB 가 새 키로 감싸여 있다면 이렇게 하세요" 라고만 적으면 유출 대응 중인
    운영자가 그 판단을 하게 되고, 틀리면 어떤 키로도 못 여는 상태가 된다.
    진단은 무대에 오른 키로 실제 DEK 를 열어 보고 답을 낸다.
    """
    staged = staged_key_path(keys_dir)
    staged.write_text(generate_master_key() + "\n", encoding="utf-8")

    # 아직 회전이 반영 안 된 경우 — 무대의 키로는 안 열린다.
    stranger = vault_from_file(staged)
    assert stranger is not None
    assert not any(stranger.can_open(w) for w in store.wrapped_deks().values())

    # 반영된 경우 — 무대의 키로 열린다.
    store.replace_wrapped_deks(
        {
            tenant: vault_for(kek).rewrap(blob, stranger)
            for tenant, blob in store.wrapped_deks().items()
        },
        actor="test",
    )
    assert all(stranger.can_open(w) for w in store.wrapped_deks().values())


def test_an_unreadable_staged_key_is_not_a_crash(keys_dir):
    """진단이 트레이스백으로 끝나면 진단이 아니다."""
    staged = staged_key_path(keys_dir)
    staged.write_text("이건 base64 가 아니다\n", encoding="utf-8")

    assert vault_from_file(staged) is None
    assert vault_from_file(keys_dir / "없는파일") is None


# ── 원자성 ──────────────────────────────────────────────────────────────────


def test_a_partial_write_would_lock_everyone_out(store, keys_dir, kek):
    """**절반만 쓰이면 그 테넌트들은 어느 키로도 안 열린다.**

    옛 키는 새로 감싼 쪽을, 새 키는 아직 옛 것인 쪽을 못 푼다. 그래서 일괄 교체가
    한 트랜잭션이어야 하고, 이 테스트는 그 트랜잭션이 실제로 되돌아가는지를 본다.
    """
    new_vault = vault_for(generate_master_key())
    old = vault_for(kek)
    rewrapped = {
        tenant: old.rewrap(blob, new_vault)
        for tenant, blob in store.wrapped_deks().items()
    }
    before = dict(store.wrapped_deks())

    original_audit = store.audit

    def fail_at_the_end(*args, **kwargs):
        raise RuntimeError("마지막 문장에서 터진다")

    store.audit = fail_at_the_end
    with pytest.raises(RuntimeError):
        store.replace_wrapped_deks(rewrapped, actor="test")
    store.audit = original_audit

    assert store.wrapped_deks() == before, "실패한 일괄 교체가 절반 남았다"


def test_the_rotation_is_audited(store, keys_dir, kek):
    """유출 대응을 되짚는 사람에게 **무엇이 사실인지** 알려 주는 유일한 근거다."""
    rotate_master_kek(store, keys_dir=keys_dir, old_vault=vault_for(kek), actor="cli:root")

    rows = store._conn.execute(
        "SELECT actor, action, detail_json FROM admin_audit "
        "WHERE action = 'rotate_master_kek'"
    ).fetchall()

    assert len(rows) == 1
    assert rows[0]["actor"] == "cli:root"
    assert "acme" in rows[0]["detail_json"]


# ── 진단이 잘못된 키를 먼저 잡는다 ──────────────────────────────────────────


def test_can_open_separates_a_wrong_key_from_a_destroyed_one(store, kek):
    """**둘을 뭉개면 파기된 테넌트가 사고로 보인다.**

    `can_open` 은 참·거짓만 주므로 호출자가 NULL 을 먼저 걸러야 하고,
    `wrapped_deks()` 가 그 걸러진 목록을 준다.
    """
    right = vault_for(kek)
    wrong = vault_for(generate_master_key())
    wrapped = store.wrapped_deks()["acme"]

    assert right.can_open(wrapped)
    assert not wrong.can_open(wrapped)
    assert "globex" in store.wrapped_deks()

    store._conn.execute("UPDATE tenants SET dek_wrapped = NULL WHERE id = 'globex'")
    store._conn.commit()
    assert "globex" not in store.wrapped_deks(), "파기된 테넌트가 목록에 남았다"


def test_opening_with_the_wrong_key_says_which_problem_it_is(store, kek):
    """"복호화 실패" 만 나오면 원인을 찾는 데 한참 걸린다."""
    wrong = vault_for(generate_master_key())

    with pytest.raises(CryptoError, match="마스터 KEK 가 다르다"):
        wrong._unwrap_bytes(store.wrapped_deks()["acme"])


# ── 런북이 실재하는 것을 가리키는가 ─────────────────────────────────────────


ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = ROOT / "docs" / "runbook-key-compromise.md"


def test_the_runbook_names_commands_that_exist():
    """**압박 속에서 읽는 문서가 없는 명령을 가리키면 최악이다.**

    런북은 평소에 아무도 안 읽는다 — 그래서 명령 이름이 바뀌어도 아무도 모른다.
    이 저장소의 다른 표들과 같은 이유로 장치를 붙인다(architecture §13-8:
    "손으로 관리하는 표는 반드시 어긋난다").
    """
    from app.cli import build_parser

    text = RUNBOOK.read_text(encoding="utf-8")
    actions = build_parser()._subparsers._group_actions[0]  # type: ignore[union-attr]
    known = set(actions.choices)

    named = {
        line.split("lcc ")[1].split()[0]
        for line in text.splitlines()
        if "lcc " in line and not line.lstrip().startswith("|")
    }
    unknown = sorted(named - known)
    assert not unknown, f"런북이 없는 명령을 가리킨다: {unknown} (있는 것: {sorted(known)})"


def test_the_runbook_names_files_that_exist():
    """`master.key.new` 같은 이름이 코드와 갈리면 복구 지시가 헛돈다."""
    text = RUNBOOK.read_text(encoding="utf-8")

    assert STAGED_NAME in text, "중단 표시 파일 이름이 런북에 없다"
    assert RETIRED_PREFIX.rstrip("-") in text, "옛 키 파일 이름이 런북에 없다"
    for path in ("app/keyrotation.py", "tests/test_keyrotation.py"):
        assert path in text
        assert (ROOT / path).exists()


def test_the_runbook_and_the_decision_agree_that_dek_rotation_is_rejected():
    """**둘이 갈리면 다음 사람이 그 기능을 다시 만든다.**

    런북은 대응 중에 읽고 결정 문서는 설계할 때 읽는다. 한쪽만 "안 만든다" 고
    적혀 있으면 다른 쪽을 읽은 사람이 만들기 시작한다.
    """
    runbook = RUNBOOK.read_text(encoding="utf-8")
    decisions = (ROOT / "docs" / "design-decisions.md").read_text(encoding="utf-8")

    assert "DEK 는 회전하지 않습니다" in runbook
    assert "DEK 회전은 **기각**한다" in decisions
    assert "보류" not in decisions.split("### D4 / G5")[1].split("### D5")[0], (
        "D4 가 구현됐는데 판정이 아직 보류다"
    )


# ── 내구성 — "디스크에 있다" 가 사실이어야 한다 ─────────────────────────────


def test_the_staged_key_is_fsynced_before_the_db_commit(store, keys_dir, kek, monkeypatch):
    """**이 절차의 안전 논거 전체가 이 순서 위에 서 있다.**

    "새 키가 DB 커밋보다 먼저 디스크에 있다" 가 회전이 어디서 죽어도 복구되는
    이유다. `write_text` 만 하면 값은 페이지 캐시에 남고, 프로세스 죽음에는
    살아남지만 **정전에는 사라진다** — 그러면 DB 만 새 래핑을 들고 남는다.
    """
    import os

    order: list[str] = []
    real_fsync = os.fsync
    real_replace = store.replace_wrapped_deks

    def watching_fsync(fd):
        order.append("fsync")
        return real_fsync(fd)

    def watching_replace(rewrapped, *, actor):
        order.append("db_commit")
        return real_replace(rewrapped, actor=actor)

    monkeypatch.setattr(os, "fsync", watching_fsync)
    store.replace_wrapped_deks = watching_replace

    rotate_master_kek(store, keys_dir=keys_dir, old_vault=vault_for(kek))

    assert "db_commit" in order, "커밋이 안 일어났다"
    assert order.index("fsync") < order.index("db_commit"), (
        f"새 키를 디스크에 굳히기 전에 DB 를 커밋했다: {order}"
    )


def test_the_rename_is_fsynced_too(store, keys_dir, kek, monkeypatch):
    """내용만 굳히고 **이름**을 안 굳히면 크래시 뒤 파일이 없을 수 있다."""
    import os

    synced_dirs: list[str] = []
    real_open, real_fsync = os.open, os.fsync

    def watching_fsync(fd):
        try:
            if os.path.isdir(f"/proc/self/fd/{fd}"):
                synced_dirs.append(str(fd))
        except OSError:
            pass
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", watching_fsync)
    assert real_open

    rotate_master_kek(store, keys_dir=keys_dir, old_vault=vault_for(kek))

    assert synced_dirs, "디렉터리를 fsync 하지 않았다 — 이름 변경이 안 굳는다"


# ── QA V1 — 이름 바꾸기 두 번 사이에서 죽은 창의 진단 ───────────────────────
#
# 파일 교체는 두 번의 이름 바꾸기다(옛 키 → 물러남, 무대 키 → 제자리). 그 사이에서
# 죽으면 `master.key` 가 **없다.** 예전 doctor 는 이 창에서 래핑을 아예 안 읽어
# (`vault.enabled` 게이트) 무조건 "회전 반영 안 됨 → rm" 을 안내했고, 그 rm 이
# DB 를 열 수 있는 유일한 키를 지운다 — 진단이 데이터 손실의 공범이 되는 경로였다.


import os
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def crashed_between_renames(tmp_path_factory):
    """부트스트랩 → 회전 → 두 rename 사이 상태 재구성. 모듈에서 한 번만 만든다."""
    base = tmp_path_factory.mktemp("v1")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    args = ["--data", str(base / "data"), "--keys", str(base / "keys")]

    subprocess.run(
        [sys.executable, "-m", "app", *args, "bootstrap"],
        cwd=ROOT, capture_output=True, text=True, timeout=60, env=env,
    )
    # 회전 전 데이터를 떠 둔다 — "백업을 복원했는데 키 디렉터리는 크래시 상태"
    # 라는 실제 운영 시나리오(물러난 키가 여는 경우)를 만들 재료다.
    shutil.copytree(base / "data", base / "data-pre")
    from app.crypto import generate_master_key

    rotated = subprocess.run(
        [sys.executable, "-m", "app", *args, "rotate-kek",
         "--new-key-env", "LCC_NEW_KEK", "--yes"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
        env={**env, "LCC_NEW_KEK": generate_master_key()},
    )
    assert rotated.returncode == 0, rotated.stderr
    # rename #1 은 됐고(물러난 키 존재) #2 는 안 된 상태로 되돌린다.
    shutil.move(str(base / "keys" / KEY_NAME), str(staged_key_path(base / "keys")))
    return base, args, env


def doctor_output(base, args, env) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "app", *args, "doctor"],
        cwd=ROOT, capture_output=True, text=True, timeout=60, env=env,
    )
    return result.stdout + result.stderr


def test_doctor_never_tells_you_to_delete_the_only_working_key(crashed_between_renames):
    """**이 창에서 rm 을 안내하면 그 rm 이 전 테넌트 원문을 지운다.**"""
    base, args, env = crashed_between_renames
    output = doctor_output(base, args, env)

    assert "이미 **새 키**로 감싸여" in output, output
    assert f"mv {staged_key_path(base / 'keys')}" in output
    assert f"rm {staged_key_path(base / 'keys')}" not in output, (
        "DB 를 열 수 있는 유일한 키를 지우라고 안내했다"
    )


def test_doctor_falls_back_to_the_retired_key(crashed_between_renames, tmp_path):
    """무대의 키가 못 열면 물러난 키로 열어 보고 **그쪽을** 안내한다.

    실제 운영 시나리오다: 회전 전 백업을 복원했는데 키 디렉터리는 크래시 상태다 —
    DB 는 옛 래핑이므로 무대의 키는 못 열고 물러난 키가 연다.
    """
    from app.keyrotation import latest_retired

    base, args, env = crashed_between_renames
    scenario = tmp_path / "retired"
    scenario.mkdir()
    shutil.copytree(base / "data-pre", scenario / "data")     # 회전 전 DB
    shutil.copytree(base / "keys", scenario / "keys")          # 크래시 상태 키

    scenario_args = ["--data", str(scenario / "data"), "--keys", str(scenario / "keys")]
    output = doctor_output(scenario, scenario_args, env)

    retired = latest_retired(scenario / "keys")
    assert retired is not None
    assert "물러난 키" in output, output
    assert f"mv {retired}" in output
    assert "rm " not in output, "이 창에서의 삭제 안내는 데이터 손실이다"


def test_doctor_refuses_to_advise_deletion_when_nothing_opens(crashed_between_renames, tmp_path):
    """어느 키로도 못 열면 **삭제를 안내하지 않는다** — 보존과 백업 복구만."""
    base, args, env = crashed_between_renames
    scenario = tmp_path / "nothing"
    shutil.copytree(base, scenario)
    keys = scenario / "keys"
    staged_key_path(keys).write_text("망가진 키\n", encoding="utf-8")
    for retired in keys.glob(f"{RETIRED_PREFIX}*"):
        retired.write_text("이것도 망가짐\n", encoding="utf-8")

    scenario_args = ["--data", str(scenario / "data"), "--keys", str(keys)]
    output = doctor_output(scenario, scenario_args, env)

    assert "아무 파일도 지우지 마세요" in output, output
    assert "rm " not in output


def test_doctor_recovery_instruction_actually_recovers(crashed_between_renames, tmp_path):
    """안내를 그대로 따라 하면 건강한 상태가 된다 — 안내문이 곧 절차다."""
    base, args, env = crashed_between_renames
    scenario = tmp_path / "recover"
    shutil.copytree(base, scenario)
    keys = scenario / "keys"
    shutil.move(str(staged_key_path(keys)), str(keys / KEY_NAME))

    scenario_args = ["--data", str(scenario / "data"), "--keys", str(keys)]
    output = doctor_output(scenario, scenario_args, env)

    assert "마스터 KEK 있음" in output
    assert "중단된 KEK 회전" not in output
    assert "열리지 않는 테넌트" not in output


# ── QA V7 — 회전 중 워커가 만든 DEK ─────────────────────────────────────────


def test_a_dek_created_after_the_snapshot_refuses_the_rotation(store, keys_dir, kek):
    """**스냅샷 이후에 생긴 DEK 가 있으면 커밋을 거부한다.**

    살아 있는 워커가 회전 중에 테넌트를 만들면 그 DEK 는 옛 키로 감싸인 채
    남고, 파일 교체 뒤 그 테넌트만 어느 키로도 못 연다. 트랜잭션 안의 집합
    재확인이 커밋 전 창을 닫는다 — 커밋 후 창은 런북의 워커 정지 단계 몫이다.
    """
    from app.store import StoreError

    real_replace = store.replace_wrapped_deks

    def worker_interferes(rewrapped, *, actor):
        # 재래핑과 커밋 사이 — 워커가 테넌트를 만든다.
        store.create_tenant(
            "newcomer", "새 테넌트", end_user_salt="s",
            dek_wrapped=vault_for(kek).create_dek(),
        )
        return real_replace(rewrapped, actor=actor)

    store.replace_wrapped_deks = worker_interferes
    with pytest.raises(StoreError, match="워커"):
        rotate_master_kek(store, keys_dir=keys_dir, old_vault=vault_for(kek))

    # 아무것도 안 바뀌었다 — 옛 키가 전부를 연다. 다시 회전하면 된다.
    store.replace_wrapped_deks = real_replace
    recovered = vault_for(kek)
    assert all(recovered.can_open(w) for w in store.wrapped_deks().values())
