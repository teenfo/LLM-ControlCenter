"""플러그인 개발 키트 — `clients/lccp.py` · 예제 플러그인 · systemd 템플릿.

이 파일이 고정하는 약속은 하나다: **`lccp check` 가 통과하면 호스트도 (매니페스트 규칙에서는) 통과한다.**
그 약속을 두 겹으로 묶는다 — **구조**(호스트 규칙 함수와 AST 가 이름마다 같다)와 **행동**(매니페스트 말뭉치에서
수락·거부와 거부 사유 문장이 호스트의 `inspect_bundle` 과 같다). 말뭉치만으로는 규칙 한 줄의 드리프트를 놓치고,
구조만으로는 도구 고유 코드(번들 만들기·서명·키)가 호스트와 맞물리는지를 못 본다.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import py_compile
import shutil
import stat
import sys
import time
import zipfile
from pathlib import Path

import pytest

from app import plugins
from app.main import VERSION
from app.plugins import CHECKSUMS_NAME, MANIFEST_NAME, SIGNATURE_NAME, PluginError
from tests.test_plugins import HOST_RANGE, MANIFEST, do_install, platform_tenant, signing_key

# pytest 는 픽스처를 모듈 이름 공간에서 찾는다 — 가져온 픽스처를 참조로 붙들어 둔다(정적 검사도 만족).
_BORROWED_FIXTURES = (platform_tenant, signing_key)

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
LCCP = ROOT / "clients" / "lccp.py"
EXAMPLES = ROOT / "examples" / "plugins"


def _load_lccp():
    spec = importlib.util.spec_from_file_location("bundled_lccp", LCCP)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def lccp():
    return _load_lccp()


def _keygen(lccp, tmp_path: Path, name: str = "acme") -> tuple[Path, Path]:
    assert lccp.main(["keygen", name, "--out", str(tmp_path / "keys")]) == 0
    return tmp_path / "keys" / f"{name}.key", tmp_path / "keys" / f"{name}.pub"


def _scaffold(lccp, tmp_path: Path, name: str = "p", *, plugin_id: str = "acme.kit-test", trigger: str = "event") -> Path:
    target = tmp_path / name
    assert lccp.main(["init", str(target), "--id", plugin_id, "--trigger", trigger]) == 0
    return target


# ── 구조 — 호스트와 같은 코드 ─────────────────────────────────────────────────

#: 호스트 파일 → `lccp.py` 에 그대로 있어야 하는 최상위 이름. 규칙을 판정하는 것 전부다.
HOST_RULES = {
    "schedule.py": [
        "_FIELDS", "_TERM", "MAX_LOOKAHEAD_DAYS", "ScheduleError", "CronSpec",
        "_parse_field", "parse_cron", "resolve_timezone", "next_after",
    ],
    "plugins.py": [
        "MANIFEST_NAME", "CHECKSUMS_NAME", "SIGNATURE_NAME", "_RESERVED",
        "MAX_BUNDLE_BYTES", "MAX_UNCOMPRESSED_BYTES", "MAX_FILES",
        "SUPPORTED_KINDS", "SUPPORTED_TRIGGERS", "SUPPORTED_EVENTS",
        "_ID", "_VERSION", "_CHECKSUM_LINE", "PluginError", "Manifest", "_require",
        "Trigger", "_parse_trigger", "parse_manifest", "_version_tuple", "host_satisfies",
        "_path_parts", "safe_names", "_read", "_key_candidates", "checksum_block",
    ],
}


def _top_level(path: Path) -> dict[str, ast.AST]:
    out: dict[str, ast.AST] = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            out[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node
    return out


def _normalized(node: ast.AST) -> str:
    """독스트링과 위치 정보를 뺀 덤프. 주석은 AST 에 없다."""
    clone = ast.parse(ast.unparse(node)).body[0]
    for inner in ast.walk(clone):
        body = getattr(inner, "body", None)
        if (
            isinstance(body, list) and body and isinstance(body[0], ast.Expr)
            and isinstance(getattr(body[0], "value", None), ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body.pop(0)
    return ast.dump(clone)


@pytest.mark.parametrize(
    "file, name", [(file, name) for file, names in HOST_RULES.items() for name in names],
)
def test_lccp_rules_are_the_host_rules_ast_for_ast(file, name):
    """규칙 한 줄이 호스트에서 바뀌면 여기가 깨진다 — `lccp check` 의 약속이 조용히 거짓이 되지 않게."""
    host = _top_level(APP / file)
    tool = _top_level(LCCP)
    assert name in tool, f"lccp.py 에 {name} 이 없다 — 호스트의 {file} 에서 그대로 옮긴다"
    assert _normalized(host[name]) == _normalized(tool[name]), (
        f"{name} 이 호스트({file})와 다르다 — 호스트를 고쳤으면 lccp.py 에도 같은 코드를 옮긴다"
    )


def test_lccp_imports_cryptography_only_inside_functions():
    """`init`·`check`·`build`(무서명)·`inspect` 는 표준 라이브러리만으로 돌아야 한다 — 서명할 때만 cryptography."""
    tree = ast.parse(LCCP.read_text(encoding="utf-8"))
    top_roots: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_roots.add(node.module.split(".")[0])
    foreign = sorted(top_roots - set(sys.stdlib_module_names))
    assert not foreign, f"lccp.py 가 최상위에서 표준 라이브러리 밖을 import 한다: {foreign}"

    crypto_imports = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and any(name.startswith("cryptography") for name in (
            [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
        ))
    ]
    assert crypto_imports, "서명은 cryptography 로 한다 — 어디서도 안 쓰면 서명이 없다"
    inside_functions = {
        id(node) for func in ast.walk(tree) if isinstance(func, ast.FunctionDef)
        for node in ast.walk(func) if isinstance(node, (ast.Import, ast.ImportFrom))
    }
    assert all(id(node) in inside_functions for node in crypto_imports), "cryptography 는 함수 안에서만 import 한다"


# ── 행동 — 매니페스트 말뭉치 ─────────────────────────────────────────────────


def _with_trigger(extra: str) -> str:
    return MANIFEST + "\n[trigger]\n" + extra


CORPUS: list[tuple[str, str]] = [
    ("기본", MANIFEST),
    ("schedule", _with_trigger('kind = "schedule"\nschedule = "0 8 * * *"\ntimezone = "Asia/Seoul"\n')),
    ("event 역할 한정", _with_trigger('kind = "event"\nevent = "job.finished"\nroles = ["summarize"]\n')),
    ("event 전체", _with_trigger('kind = "event"\nevent = "job.finished"\n')),
    ("범위 없음", MANIFEST.replace(f'requires_host = "{HOST_RANGE}"\n', "")),
    ("프리릴리스 버전", MANIFEST.replace('version = "1.0.0"', 'version = "1.0.0-rc.1"')),
    ("예산 0", MANIFEST.replace("budget_usd_per_month = 5.0", "budget_usd_per_month = 0")),
    ("id 형식", MANIFEST.replace('id = "acme.daily-digest"', 'id = "notreversedns"')),
    ("실행 형태", MANIFEST.replace('kind = "external"', 'kind = "native"')),
    ("역할 없음", MANIFEST.replace('allow_roles = ["summarize"]', "allow_roles = []")),
    ("내부 역할", MANIFEST.replace('["summarize"]', '["_guard_classify"]')),
    ("런타임 요구", MANIFEST.replace("[run]\n", '[run]\nrequires_runtime = "node20"\n')),
    ("endpoint", MANIFEST.replace("http://acme-digest:9000", "ftp://acme-digest")),
    ("rate 0", MANIFEST.replace("rate_limit_per_min = 10", "rate_limit_per_min = 0")),
    ("예산 음수", MANIFEST.replace("budget_usd_per_month = 5.0", "budget_usd_per_month = -1")),
    ("모르는 트리거", _with_trigger('kind = "webhook"\n')),
    ("영원히 안 도는 스케줄", _with_trigger('kind = "schedule"\nschedule = "0 0 30 2 *"\n')),
    ("네 칸 cron", _with_trigger('kind = "schedule"\nschedule = "0 8 * *"\n')),
    ("모르는 시간대", _with_trigger('kind = "schedule"\nschedule = "0 8 * * *"\ntimezone = "Mars/Olympus"\n')),
    ("모르는 이벤트", _with_trigger('kind = "event"\nevent = "node.offline"\n')),
    ("이벤트 내부 역할", _with_trigger('kind = "event"\nevent = "job.finished"\nroles = ["_guard_classify"]\n')),
    ("호스트 범위 밖", MANIFEST.replace(f'requires_host = "{HOST_RANGE}"', 'requires_host = ">=9.0"')),
    ("범위 문법", MANIFEST.replace(f'requires_host = "{HOST_RANGE}"', 'requires_host = "~=0.1"')),
    ("TOML 깨짐", MANIFEST + "\n[service\n"),
    ("[service] 없음", MANIFEST.replace("[service]", "[svc]")),
]


def test_the_corpus_is_big_enough_to_mean_something():
    assert len(CORPUS) >= 15
    assert len({label for label, _ in CORPUS}) == len(CORPUS)


@pytest.mark.parametrize("label, manifest", CORPUS, ids=[label for label, _ in CORPUS])
def test_lccp_check_agrees_with_the_host_on_the_manifest_corpus(lccp, tmp_path, capsys, label, manifest):
    """수락/거부와 거부 사유 문장이 호스트의 `inspect_bundle` 과 같다 — 디렉터리로 봐도, 번들로 봐도."""
    source = tmp_path / "p"
    source.mkdir()
    (source / MANIFEST_NAME).write_text(manifest, encoding="utf-8")
    (source / "main.py").write_text("print('hi')\n", encoding="utf-8")
    raw = lccp.pack(lccp.files_under(source)[0])
    bundle_path = tmp_path / "p.lccp"
    bundle_path.write_bytes(raw)

    try:
        plugins.inspect_bundle(raw, trust_dir=tmp_path / "trust", host_version=VERSION, require_signature=False)
        verdict = None
    except PluginError as exc:
        verdict = str(exc)

    for target in (source, bundle_path):
        code = lccp.main(["check", str(target), "--host-version", VERSION])
        captured = capsys.readouterr()
        if verdict is None:
            assert code == 0, f"{label}: 호스트는 받는데 lccp 가 거부했다 — {captured.err}"
        else:
            assert code == 1, f"{label}: 호스트는 거부하는데 lccp 가 받았다 — 호스트 사유: {verdict}"
            assert verdict in captured.err, f"{label}: 사유 문장이 다르다\n  호스트: {verdict}\n  lccp: {captured.err}"


def test_lccp_parses_the_manifest_into_the_same_fields_as_the_host(lccp):
    """수락된 매니페스트의 판독 결과(역할·한도·트리거·시간대)가 필드 단위로 같다."""
    manifest = _with_trigger('kind = "schedule"\nschedule = "0 8 * * 1-5"\ntimezone = "Asia/Seoul"\n')
    host = plugins.parse_manifest(manifest.encode("utf-8"))
    tool = lccp.parse_manifest(manifest.encode("utf-8"))
    for name in ("plugin_id", "name", "version", "kind", "requires_host", "endpoint", "allow_roles",
                 "rate_limit_per_min", "budget_usd_per_month", "schedule", "schedule_tz", "event", "event_roles"):
        assert getattr(host, name) == getattr(tool, name), name
    assert host.service_fields() == tool.service_fields()


# ── 키 · 번들 · 서명 — 호스트와 맞물리는가 ────────────────────────────────────


def test_lccp_keygen_public_key_is_read_by_the_trust_dir(lccp, tmp_path, capsys):
    """`keygen` 의 `.pub`(hex + 개행)를 호스트의 `load_trusted_keys` 가 그대로 읽는다. 비밀 키는 0600 이다."""
    key, pub = _keygen(lccp, tmp_path)
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    text = pub.read_text(encoding="ascii")
    assert len(text.strip()) == 64 and text.endswith("\n"), "hex 텍스트 + 개행 — 날바이트는 cat·붙여넣기에서 잘린다"
    loaded = plugins.load_trusted_keys(pub.parent)
    assert [k.public_bytes_raw().hex() for k in loaded] == [text.strip()]
    # 키는 덮어쓰지 않는다 — 배포된 공개 키와 어긋난 비밀 키는 아무것도 서명하지 못한다.
    assert lccp.main(["keygen", "acme", "--out", str(tmp_path / "keys")]) == 2
    assert "덮어쓰지 않는다" in capsys.readouterr().err


def test_lccp_build_is_accepted_by_the_host_as_signed(lccp, tmp_path, harness, platform_tenant):
    """`build --sign` 한 번들을 호스트가 `signed` 로 읽고 설치한다 — 서명 배치가 호스트와 바이트 호환이다."""
    key, pub = _keygen(lccp, tmp_path)
    harness.trust_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pub, harness.trust_dir / pub.name)
    source = _scaffold(lccp, tmp_path)
    out = tmp_path / "out.lccp"
    assert lccp.main(["build", str(source), "-o", str(out), "--sign", str(key)]) == 0

    raw = out.read_bytes()
    inspected = plugins.inspect_bundle(raw, trust_dir=harness.trust_dir, host_version=VERSION)
    assert inspected.signature_state == "signed"
    assert set(inspected.payload) == {MANIFEST_NAME, "main.py", "README.md"}
    installed = do_install(harness, raw)
    assert installed.signature_state == "signed" and installed.token

    # `sign` 은 만들어 둔 무서명 번들에도 같은 서명을 건다.
    unsigned = tmp_path / "unsigned.lccp"
    assert lccp.main(["build", str(source), "-o", str(unsigned)]) == 0
    assert lccp.main(["sign", str(unsigned), "--key", str(key)]) == 0
    assert unsigned.read_bytes() == raw, "같은 입력 · 같은 키 → 같은 바이트"


def test_lccp_builds_are_byte_identical(lccp, tmp_path):
    """같은 입력이면 같은 바이트다 — 파일 mtime 이 달라도. 운영자가 sha256 을 비교할 수 있어야 한다."""
    key, _pub = _keygen(lccp, tmp_path)
    source = _scaffold(lccp, tmp_path)
    first, second = tmp_path / "a.lccp", tmp_path / "b.lccp"
    assert lccp.main(["build", str(source), "-o", str(first), "--sign", str(key)]) == 0
    yesterday = time.time() - 86400
    for path in source.rglob("*"):
        os.utime(path, (yesterday, yesterday))
    assert lccp.main(["build", str(source), "-o", str(second), "--sign", str(key)]) == 0
    assert first.read_bytes() == second.read_bytes()

    with zipfile.ZipFile(first) as archive:
        infos = archive.infolist()
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in infos), "고정 시각"
        names = [info.filename for info in infos]
        payload = [name for name in names if name not in (CHECKSUMS_NAME, SIGNATURE_NAME)]
        assert names == sorted(payload) + [CHECKSUMS_NAME, SIGNATURE_NAME], "정렬 · 예약 파일은 마지막"
        files = {name: archive.read(name) for name in payload}
        # 체크섬 한 장은 호스트의 `checksum_block` 이 만드는 것과 같은 바이트다 — 서명이 덮는 것이 그것이다.
        assert archive.read(CHECKSUMS_NAME) == plugins.checksum_block(files)
        assert len(archive.read(SIGNATURE_NAME)) == 128, "hex 서명"


def _tamper(source: Path, target: Path) -> None:
    with zipfile.ZipFile(source) as archive:
        entries = [(info, archive.read(info.filename)) for info in archive.infolist()]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for info, data in entries:
            archive.writestr(info, data + b"# tampered\n" if info.filename == "main.py" else data)


def test_lccp_verify_reports_like_the_host(lccp, tmp_path, capsys):
    """signed · unsigned · invalid — 호스트의 `verify_bundle` 과 같은 말을 한다. 신뢰 키 없이는 판정하지 않는다."""
    key, pub = _keygen(lccp, tmp_path)
    source = _scaffold(lccp, tmp_path)
    signed, unsigned, tampered = tmp_path / "s.lccp", tmp_path / "u.lccp", tmp_path / "t.lccp"
    assert lccp.main(["build", str(source), "-o", str(signed), "--sign", str(key)]) == 0
    assert lccp.main(["build", str(source), "-o", str(unsigned)]) == 0
    _tamper(signed, tampered)
    trust = tmp_path / "trust"
    trust.mkdir()
    shutil.copyfile(pub, trust / pub.name)

    for path, expected, code in ((signed, "signed", 0), (unsigned, "unsigned", 1), (tampered, "invalid", 1)):
        with zipfile.ZipFile(path) as archive:
            names = plugins.safe_names(archive)
            assert plugins.verify_bundle(archive, names, trust) == expected
        assert lccp.main(["verify", str(path), "--trust", str(trust)]) == code, path.name
        captured = capsys.readouterr()
        if expected == "invalid":
            assert "번들 검증에 실패했습니다" in captured.err, "호스트와 같은 거부 문장"
        else:
            assert f"signature     {expected}" in captured.out

    # `--pub` 한 장으로도 같다.
    assert lccp.main(["verify", str(signed), "--pub", str(pub)]) == 0
    capsys.readouterr()
    # 신뢰 키를 주지 않으면 호스트의 `invalid` 와 다른 이름(`unverified`)으로 말하고 2 로 끝난다.
    assert lccp.main(["verify", str(signed)]) == 2
    captured = capsys.readouterr()
    assert "unverified" in captured.out and "판정 불가" in captured.err
    # 변조된 번들은 신뢰 키가 없어도 해시가 어긋난다 — 그것은 판정할 수 있다.
    assert lccp.main(["inspect", str(tampered)]) == 1
    assert "번들 검증에 실패했습니다" in capsys.readouterr().err


@pytest.mark.parametrize("trigger", ["schedule", "event", "none"])
def test_lccp_init_scaffold_passes_check_and_builds(lccp, tmp_path, trigger, capsys):
    """`init` 이 만든 것은 곧바로 `check` 를 지나고, 만들어지고, 호스트가 읽는다 — 시작점이 거짓이면 안 된다."""
    plugin_id = f"acme.{trigger}-demo"
    source = _scaffold(lccp, tmp_path, plugin_id=plugin_id, trigger=trigger)
    assert lccp.main(["check", str(source)]) == 0, capsys.readouterr().err
    py_compile.compile(str(source / "main.py"), doraise=True)
    ast.parse((source / "main.py").read_text(encoding="utf-8"), feature_version=(3, 9))

    manifest = plugins.parse_manifest((source / MANIFEST_NAME).read_bytes())
    assert manifest.plugin_id == plugin_id
    assert (manifest.schedule is not None) is (trigger == "schedule")
    assert (manifest.event is not None) is (trigger == "event")
    assert plugins.host_satisfies(VERSION, manifest.requires_host), manifest.requires_host

    out = tmp_path / "x.lccp"
    assert lccp.main(["build", str(source), "-o", str(out)]) == 0
    inspected = plugins.inspect_bundle(
        out.read_bytes(), trust_dir=tmp_path / "none", host_version=VERSION, require_signature=False,
    )
    assert inspected.signature_state == "unsigned"
    assert set(inspected.payload) == {MANIFEST_NAME, "main.py", "README.md"}

    # 비어 있지 않은 디렉터리와 틀린 id 는 사용법 오류(2)다 — 아무것도 덮어쓰지 않는다.
    before = sorted(p.name for p in source.iterdir())
    assert lccp.main(["init", str(source), "--id", "acme.other"]) == 2
    assert sorted(p.name for p in source.iterdir()) == before
    assert lccp.main(["init", str(tmp_path / "bad"), "--id", "nodots"]) == 2
    assert not (tmp_path / "bad").exists()


def test_lccp_refuses_symlinks_and_oversized_payloads(lccp, tmp_path, monkeypatch, capsys):
    """호스트가 zip 안에서 거부할 것을 **만들 때** 거른다 — 올려서 거절당하는 것보다 싸다. 비밀 키는 담지 않는다."""
    source = _scaffold(lccp, tmp_path)
    out = tmp_path / "o.lccp"
    (tmp_path / "outside").write_text("secret", encoding="utf-8")
    (source / "leak").symlink_to(tmp_path / "outside")
    assert lccp.main(["build", str(source), "-o", str(out)]) == 1
    assert "심볼릭 링크" in capsys.readouterr().err and not out.exists()
    (source / "leak").unlink()

    real_files, real_bytes = lccp.MAX_FILES, lccp.MAX_UNCOMPRESSED_BYTES
    monkeypatch.setattr(lccp, "MAX_FILES", 2)
    assert lccp.main(["check", str(source)]) == 1
    assert "너무 많습니다" in capsys.readouterr().err
    monkeypatch.setattr(lccp, "MAX_FILES", real_files)
    monkeypatch.setattr(lccp, "MAX_UNCOMPRESSED_BYTES", 64)
    assert lccp.main(["build", str(source), "-o", str(out)]) == 1
    assert "해제 크기" in capsys.readouterr().err and not out.exists()
    monkeypatch.setattr(lccp, "MAX_UNCOMPRESSED_BYTES", real_bytes)

    (source / "acme.key").write_text("deadbeef" * 8 + "\n", encoding="ascii")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "main.cpython-311.pyc").write_bytes(b"\x00")
    assert lccp.main(["build", str(source), "-o", str(out)]) == 0
    printed = capsys.readouterr().out
    with zipfile.ZipFile(out) as archive:
        assert "acme.key" not in archive.namelist() and not any("__pycache__" in n for n in archive.namelist())
    assert "뺐다: acme.key" in printed


def test_lccp_exit_codes_tell_usage_from_rejection(lccp, tmp_path, capsys):
    assert lccp.main(["check", str(tmp_path / "missing")]) == 2
    assert lccp.main(["verify", str(tmp_path / "missing.lccp")]) == 2
    (tmp_path / "junk.lccp").write_bytes(b"not a zip")
    assert lccp.main(["check", str(tmp_path / "junk.lccp")]) == 1
    assert "zip 이 아닙니다" in capsys.readouterr().err
    assert lccp.main([]) == 0, "인자 없이 부르면 도움말이고 오류가 아니다"


# ── 예제 플러그인 · systemd 템플릿 ────────────────────────────────────────────

EXAMPLE_DIRS = sorted(path.name for path in EXAMPLES.iterdir() if (path / MANIFEST_NAME).is_file())


def test_the_examples_show_both_triggers():
    triggers = set()
    for name in EXAMPLE_DIRS:
        manifest = plugins.parse_manifest((EXAMPLES / name / MANIFEST_NAME).read_bytes())
        triggers.add("schedule" if manifest.schedule else "event" if manifest.event else "none")
    assert triggers == {"schedule", "event"}, "예제는 두 트리거를 하나씩 보여 준다"


@pytest.mark.parametrize("example", EXAMPLE_DIRS)
def test_the_example_manifests_pass_the_host_rules_and_target_this_host(lccp, example, capsys):
    """예제는 교재다 — 이 호스트 판에 실제로 설치되고, 3.9 문법으로 써 있고, `check` 를 지난다."""
    folder = EXAMPLES / example
    manifest = plugins.parse_manifest((folder / MANIFEST_NAME).read_bytes())
    assert manifest.requires_host, "예제는 호스트 범위를 박는다 — 아무 판에나 붙는 교재는 거짓말이다"
    assert plugins.host_satisfies(VERSION, manifest.requires_host), (VERSION, manifest.requires_host)
    assert manifest.plugin_id.startswith("example."), "예제 id 는 example. 네임스페이스"

    source = (folder / "main.py").read_text(encoding="utf-8")
    ast.parse(source, feature_version=(3, 9))          # 플러그인은 다른 기계에서 돈다
    py_compile.compile(str(folder / "main.py"), doraise=True)
    assert "LCC_SDK_DIR" in source and "STATE_DIRECTORY" in source
    assert (folder / "README.md").is_file()
    assert lccp.main(["check", str(folder)]) == 0, capsys.readouterr().err


def test_finish_log_records_metadata_only_by_default(monkeypatch):
    """마스킹본이라도 테넌트의 글이 남의 저널에 남는 것은 기본값이 아니다 — 본문은 `--with-text` 로만."""
    monkeypatch.setenv("LCC_SDK_DIR", str(ROOT / "clients"))
    for name in ("plugin", "finish_log_example"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("finish_log_example", EXAMPLES / "finish-log" / "main.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        sdk = sys.modules["plugin"]
        event = sdk.Event.from_body({
            "id": 7, "kind": "job.finished", "ts": 1.0, "job_id": "j1", "role": "summarize", "status": "ok",
            "prompt": "마스킹된 프롬프트", "system": "시스템", "output": "응답 본문", "error": None,
            "usage": {"input_tokens": 3}, "started_at": 1.0, "finished_at": 2.5, "model": "m", "node": "n",
        })
        plain = module.row_for(event)
        assert not ({"prompt", "system", "output"} & set(plain)), "기본은 본문을 쓰지 않는다"
        assert plain["role"] == "summarize" and plain["latency"] == 1.5 and plain["usage"] == {"input_tokens": 3}
        full = module.row_for(event, with_text=True)
        assert full["prompt"] == "마스킹된 프롬프트" and full["output"] == "응답 본문"
    finally:
        for name in ("plugin", "finish_log_example"):
            sys.modules.pop(name, None)


def test_the_systemd_template_has_no_end_of_line_comments():
    """systemd 는 `Key=value   # 주석` 의 주석을 값으로 읽는다 — 감사 타이머에서 실제로 `Persistent` 가 무시됐다."""
    text = (EXAMPLES / "systemd" / "llmcc-plugin@.service").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("["):
            continue
        key, _, value = stripped.partition("=")
        assert key and "#" not in value, f"값 뒤 주석은 값의 일부가 된다: {line!r}"
    assert "EnvironmentFile=/etc/llmcc-plugins/%i.env" in text, "토큰은 600 환경 파일에만"
    assert "Restart=on-failure" in text and "RestartSec=" in text
    assert "User=root" not in text and "DynamicUser=yes" in text, "비루트로 돈다"
    assert "StateDirectory=" in text, "쓸 수 있는 곳을 systemd 가 준다 — 예제들이 $STATE_DIRECTORY 에 쓴다"
