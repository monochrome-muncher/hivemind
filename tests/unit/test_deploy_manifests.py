"""Hermetic checks on the deployment artifacts (DEP-1..DEP-15).

No cluster, no docker: parse every manifest, syntax-check every shell
fragment, and run the image entrypoint and the key-bootstrap Job script
against stubs. These catch the classes of bug that only ever surfaced on
the first real deploy (a heredoc terminator eaten by YAML indentation, a
masked CLI failure, a missing `generic`).
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
K8S = ROOT / "deploy" / "kubernetes"
WORKLOADS = [
    K8S / "hivemind-api-deployment.yaml",
    K8S / "hivemind-mcp-deployment.yaml",
    K8S / "admin" / "hivemind-admin-deployment.yaml",
    K8S / "bootstrap" / "keys-job.yaml",
]


def _load(path: Path) -> dict[str, Any]:
    doc = yaml.safe_load(path.read_text())
    assert isinstance(doc, dict)
    return doc


def _pod_spec(doc: dict[str, Any]) -> dict[str, Any]:
    return doc["spec"]["template"]["spec"]  # type: ignore[no-any-return]


def _shell_syntax_ok(shell: str, script: str) -> None:
    proc = subprocess.run([shell, "-n"], input=script, text=True, capture_output=True)
    assert proc.returncode == 0, f"{shell} -n failed:\n{proc.stderr}\n--- script ---\n{script}"


def test_every_manifest_parses() -> None:
    for path in [*sorted(K8S.rglob("*.yaml")), ROOT / "docker-compose.yaml"]:
        docs = list(yaml.safe_load_all(path.read_text()))
        assert docs, path


# --- DEP-1 / DEP-2: the CI scripts ------------------------------------------------


def _ci_scripts() -> dict[str, str]:
    ci = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())
    out: dict[str, str] = {}
    for name, job in ci.items():
        if not isinstance(job, dict):
            continue
        for key in ("before_script", "script", "after_script"):
            if key in job:
                out[f"{name}.{key}"] = "\n".join(job[key])
    return out


@pytest.mark.parametrize("name", sorted(_ci_scripts()))
def test_gitlab_ci_scripts_are_valid_shell(name: str) -> None:
    # DEP-1: the old inline heredoc lost its terminator to YAML indentation.
    _shell_syntax_ok("bash", _ci_scripts()[name])


def test_ci_helper_scripts_are_valid_posix_shell() -> None:
    for script in (ROOT / "scripts" / "ci-bootstrap-keys.sh", ROOT / "entrypoint.sh"):
        _shell_syntax_ok("sh", script.read_text())
        _shell_syntax_ok("dash", script.read_text())


def test_no_inline_heredoc_manifest_in_ci() -> None:
    assert "<<EOF" not in (ROOT / ".gitlab-ci.yml").read_text()


def test_create_secret_always_names_a_subtype() -> None:
    # DEP-2: `kubectl create secret <name>` is an error; it needs `generic`.
    for path in (
        ROOT / ".gitlab-ci.yml",
        ROOT / "scripts" / "ci-bootstrap-keys.sh",
        K8S / "secret-template.yaml",
        ROOT / "DEPLOY.md",
    ):
        for line in path.read_text().splitlines():
            m = re.search(r"create secret (\S+)", line)
            if m:
                assert m.group(1) in {"generic", "docker-registry", "tls"}, (path, line)


def test_no_bogus_kustomize_build_form() -> None:
    # DEP-11e: `kubectl kustomize build <dir>` passes a literal "build" arg.
    for path in [*ROOT.glob("*.md"), *K8S.rglob("*.yaml"), ROOT / ".gitlab-ci.yml"]:
        assert "kubectl kustomize build" not in path.read_text(), path


def test_ci_images_are_pinned_and_deploy_is_serialised() -> None:
    text = (ROOT / ".gitlab-ci.yml").read_text()
    assert ":latest" not in text
    ci = yaml.safe_load(text)
    assert ci["deploy"]["resource_group"] == "production"
    assert '-p "$CI_REGISTRY_PASSWORD"' not in text
    assert "--password-stdin" in text


# --- DEP-9: the bootstrap Job and its script -------------------------------------


def _job_script() -> str:
    job = _load(K8S / "bootstrap" / "keys-job.yaml")
    container = _pod_spec(job)["containers"][0]
    assert container["command"][:2] == ["sh", "-c"]
    return str(container["command"][2])


def test_bootstrap_job_shape() -> None:
    job = _load(K8S / "bootstrap" / "keys-job.yaml")
    assert job["kind"] == "Job"
    assert job["spec"]["backoffLimit"] == 0  # a failure surfaces in CI, never silently retried
    assert "ttlSecondsAfterFinished" in job["spec"]
    container = _pod_spec(job)["containers"][0]
    refs = {(next(iter(e)), next(iter(e.values()))["name"]) for e in container["envFrom"]}
    assert ("secretRef", "hivemind-secrets") in refs
    assert ("configMapRef", "hivemind-config") in refs  # the embedding dim lives there


def test_bootstrap_job_runs_as_its_own_narrow_service_account() -> None:
    # ADR 0044: the Job is the only workload that mounts a token, and its
    # Role can create Secrets and get / delete hivemind-keys - nothing else.
    pod = _pod_spec(_load(K8S / "bootstrap" / "keys-job.yaml"))
    assert pod["serviceAccountName"] == "hivemind-keys-bootstrap"
    assert pod["automountServiceAccountToken"] is True
    docs = {d["kind"]: d for d in yaml.safe_load_all((K8S / "bootstrap" / "rbac.yaml").read_text())}
    assert set(docs) == {"ServiceAccount", "Role", "RoleBinding"}
    assert docs["ServiceAccount"]["automountServiceAccountToken"] is False
    rules = docs["Role"]["rules"]
    assert all(r["apiGroups"] == [""] and r["resources"] == ["secrets"] for r in rules)
    granted = {(v, tuple(r.get("resourceNames", []))) for r in rules for v in r["verbs"]}
    assert granted == {
        ("create", ()),
        ("get", ("hivemind-keys",)),
        ("delete", ("hivemind-keys",)),
    }
    binding = docs["RoleBinding"]
    assert binding["roleRef"]["name"] == docs["Role"]["metadata"]["name"]
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "hivemind-keys-bootstrap", "namespace": "hivemind"}
    ]


def test_bootstrap_job_script_is_valid_shell() -> None:
    _shell_syntax_ok("sh", _job_script())


def _run_job_script(tmp_path: Path, keys_stub: str) -> subprocess.CompletedProcess[str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in {"hivemind-migrate": "exit 0", "hivemind-keys": keys_stub}.items():
        stub = bindir / name
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    return subprocess.run(
        ["sh", "-c", _job_script()], env=env, text=True, capture_output=True, timeout=30
    )


def test_bootstrap_job_writes_the_secret_and_prints_no_key(tmp_path: Path) -> None:
    calls = tmp_path / "keys-calls"
    proc = _run_job_script(tmp_path, f'echo "$*" >> {calls}; echo "issued (not printed)"')
    assert proc.returncode == 0, proc.stderr
    assert calls.read_text().split() == [
        "--actor",
        "ci-bootstrap",
        "bootstrap-secret",
        "--secret",
        "hivemind-keys",
    ]
    # The old Job echoed ADMIN_KEY= / ORG_KEY= for the CI script to scrape.
    assert "KEY=" not in _job_script()


def test_bootstrap_job_fails_when_the_cli_fails(tmp_path: Path) -> None:
    proc = _run_job_script(tmp_path, "exit 1")
    assert proc.returncode != 0


def _bootstrap_with_fake_kubectl(
    tmp_path: Path, **extra_env: str
) -> subprocess.CompletedProcess[str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls"
    created = tmp_path / "created"
    kubectl = bindir / "kubectl"
    kubectl.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            echo "$*" >> {calls}
            case "$*" in
              *"get secret hivemind-keys"*)
                [ -n "$FAKE_API_ERROR" ] && {{ echo "Unable to connect to the server" >&2; exit 1; }}
                [ -n "$FAKE_SECRET_EXISTS" ] && echo secret/hivemind-keys
                [ -f {created} ] && echo secret/hivemind-keys
                exit 0 ;;
              *"get job"*succeeded*)
                [ -n "$FAKE_JOB_FAILS" ] && exit 0
                [ -z "$FAKE_NO_SECRET" ] && touch {created}
                echo 1 ;;
              *"get job"*failed*) [ -n "$FAKE_JOB_FAILS" ] && echo 1 ;;
              *" logs "*) echo "Kubernetes API create secret failed: HTTP 403" ;;
            esac
            exit 0
            """
        )
    )
    kubectl.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "IMAGE": "reg/hivemind:abc",
        **extra_env,
    }
    return subprocess.run(
        ["sh", str(ROOT / "scripts" / "ci-bootstrap-keys.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )


def test_ci_bootstrap_runs_the_job_and_removes_its_rbac(tmp_path: Path) -> None:
    proc = _bootstrap_with_fake_kubectl(tmp_path)
    assert proc.returncode == 0, proc.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    applies = [i for i, c in enumerate(calls) if c.endswith("apply -f -")]
    deletes = [i for i, c in enumerate(calls) if "delete --ignore-not-found=true -f -" in c]
    assert len(applies) == 2  # the RBAC, then the Job
    assert deletes and deletes[-1] > applies[-1]  # RBAC removed after the Job
    # The script never reads keys and never creates the Secret itself.
    assert not any(" logs " in c or "create secret" in c for c in calls)
    assert "hivemind-keys secret created" in proc.stdout


def test_ci_bootstrap_shows_the_log_and_removes_rbac_when_the_job_fails(tmp_path: Path) -> None:
    proc = _bootstrap_with_fake_kubectl(tmp_path, FAKE_JOB_FAILS="1")
    assert proc.returncode != 0
    assert "HTTP 403" in proc.stderr  # the log holds no key (ADR 0044), so it is shown
    calls = (tmp_path / "calls").read_text()
    assert "delete --ignore-not-found=true -f -" in calls


def test_ci_bootstrap_fails_when_the_job_left_no_secret(tmp_path: Path) -> None:
    proc = _bootstrap_with_fake_kubectl(tmp_path, FAKE_NO_SECRET="1")
    assert proc.returncode != 0
    assert "missing" in proc.stderr


def test_ci_bootstrap_aborts_on_an_api_error_instead_of_assuming_absent(tmp_path: Path) -> None:
    proc = _bootstrap_with_fake_kubectl(tmp_path, FAKE_API_ERROR="1")
    assert proc.returncode != 0
    calls = (tmp_path / "calls").read_text()
    assert "apply" not in calls
    assert "delete job" not in calls


def test_ci_bootstrap_skips_when_the_secret_exists(tmp_path: Path) -> None:
    proc = _bootstrap_with_fake_kubectl(tmp_path, FAKE_SECRET_EXISTS="1")
    assert proc.returncode == 0
    assert "apply" not in (tmp_path / "calls").read_text()


def test_ci_bootstrap_rewrites_the_rolebinding_namespace() -> None:
    rbac = (K8S / "bootstrap" / "rbac.yaml").read_text()
    script = (ROOT / "scripts" / "ci-bootstrap-keys.sh").read_text()
    # The script's sed must hit exactly the RoleBinding subject's namespace line.
    assert rbac.count("namespace: hivemind\n") == 1
    assert "s|namespace: hivemind\\$|namespace: $NS|" in script


# --- DEP-4 / DEP-5 / DEP-12: workload hardening ------------------------------------


@pytest.mark.parametrize("path", WORKLOADS, ids=lambda p: p.name)
def test_workloads_are_hardened(path: Path) -> None:
    pod = _pod_spec(_load(path))
    # The bootstrap Job alone mounts its own narrow token (ADR 0044).
    assert pod["automountServiceAccountToken"] is (path.name == "keys-job.yaml")
    psc = pod["securityContext"]
    assert psc["runAsNonRoot"] is True
    assert isinstance(psc["runAsUser"], int) and psc["runAsUser"] > 0
    assert psc["seccompProfile"] == {"type": "RuntimeDefault"}
    for c in pod["containers"]:
        sc = c["securityContext"]
        assert sc["allowPrivilegeEscalation"] is False
        assert sc["readOnlyRootFilesystem"] is True
        assert sc["capabilities"] == {"drop": ["ALL"]}
        mounts = {m["mountPath"] for m in c.get("volumeMounts", [])}
        assert "/tmp" in mounts  # the only writable path


@pytest.mark.parametrize("path", WORKLOADS[:3], ids=lambda p: p.name)
def test_deployments_declare_resources(path: Path) -> None:
    res = _pod_spec(_load(path))["containers"][0]["resources"]
    assert res["requests"]["cpu"] and res["requests"]["memory"]
    assert res["limits"]["memory"]


@pytest.mark.parametrize("path", WORKLOADS[:2], ids=lambda p: p.name)
def test_two_replica_deployments_spread_across_nodes(path: Path) -> None:
    pod = _pod_spec(_load(path))
    (tsc,) = pod["topologySpreadConstraints"]
    assert tsc["topologyKey"] == "kubernetes.io/hostname"
    assert tsc["whenUnsatisfiable"] == "ScheduleAnyway"  # preferred, never blocks a rollout


def test_optional_tree_has_network_policies_and_body_limit() -> None:
    # The policies are their own opt-in tree, NOT bundled with the Ingress.
    assert "networkpolicy.yaml" not in _load(K8S / "optional" / "kustomization.yaml")["resources"]
    kust = _load(K8S / "optional" / "networkpolicy" / "kustomization.yaml")
    assert kust["resources"] == ["networkpolicy.yaml"]
    docs = list(
        yaml.safe_load_all((K8S / "optional" / "networkpolicy" / "networkpolicy.yaml").read_text())
    )
    assert docs and all(d["kind"] == "NetworkPolicy" for d in docs)
    ingress = next(
        d
        for d in yaml.safe_load_all((K8S / "optional" / "ingress.yaml").read_text())
        if d["kind"] == "Ingress"
    )
    assert "nginx.ingress.kubernetes.io/proxy-body-size" in ingress["metadata"]["annotations"]


def test_runner_typo_is_gone() -> None:
    assert "HIVMIND" not in (K8S / "hivemind-api-deployment.yaml").read_text()


# --- DEP-3 / DEP-4 / DEP-7: the image, compose, the workflow -------------------------


def test_dockerfile_installs_from_the_lock_and_drops_root() -> None:
    text = (ROOT / "Dockerfile").read_text()
    assert "uv sync --locked" in text and "--no-dev" in text
    assert "--frozen" not in text  # --frozen would not notice pyproject/lock drift
    assert not re.search(r"pip install[^\n]*\s\.\s*$", text, re.M)  # the floating `pip install .`
    user = re.findall(r"^USER\s+(\S+)", text, re.M)
    assert user
    uid = user[-1].split(":")[0]
    assert uid.isdigit()
    assert uid != "0"
    assert "HIVEMIND_RUNNER" in text


def test_compose_publishes_databases_on_loopback_only() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yaml").read_text())
    for svc in ("postgres", "vllm"):
        for port in compose["services"][svc]["ports"]:
            assert str(port).startswith("127.0.0.1:"), (svc, port)


def test_github_workflow_hardening() -> None:
    text = (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text()
    assert re.search(r"actions/checkout@[0-9a-f]{40}", text)
    wf = yaml.safe_load(text)
    # Write scopes (packages / id-token) never reach a pull_request run.
    assert "id-token" not in (wf.get("permissions") or {})
    assert "packages" not in (wf.get("permissions") or {})
    for name, job in wf["jobs"].items():
        perms = job.get("permissions") or {}
        if perms.get("packages") == "write" or perms.get("id-token") == "write":
            assert "github.event_name != 'pull_request'" in job.get("if", ""), name


# --- DEP-14: the entrypoint ----------------------------------------------------------


def _entrypoint_env(tmp_path: Path, migrate_body: str, runner_body: str) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in {"hivemind-migrate": migrate_body, "hivemind-mcp-http": runner_body}.items():
        stub = bindir / name
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(0o755)
    return {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}


def test_entrypoint_migrates_then_execs_the_runner(tmp_path: Path) -> None:
    env = _entrypoint_env(tmp_path, "echo migrated", 'echo "runner $*"')
    proc = subprocess.run(
        ["sh", str(ROOT / "entrypoint.sh"), "--flag"], env=env, text=True, capture_output=True
    )
    assert proc.returncode == 0
    assert proc.stdout.split() == ["migrated", "runner", "--flag"]


def test_entrypoint_aborts_when_migration_fails(tmp_path: Path) -> None:
    env = _entrypoint_env(tmp_path, "exit 3", "echo SHOULD-NOT-RUN")
    proc = subprocess.run(
        ["sh", str(ROOT / "entrypoint.sh")], env=env, text=True, capture_output=True
    )
    assert proc.returncode == 3
    assert "SHOULD-NOT-RUN" not in proc.stdout


def test_entrypoint_sigterm_during_migration_exits_promptly(tmp_path: Path) -> None:
    env = _entrypoint_env(tmp_path, "exec sleep 60", "echo SHOULD-NOT-RUN")
    proc = subprocess.Popen(
        ["sh", str(ROOT / "entrypoint.sh")], env=env, text=True, stdout=subprocess.PIPE
    )
    time.sleep(1.0)  # let the migrate stub start
    proc.send_signal(signal.SIGTERM)
    try:
        rc = proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert rc == 143
    assert proc.stdout is not None
    assert "SHOULD-NOT-RUN" not in proc.stdout.read()


def test_entrypoint_migrate_runner_passes_args_and_status(tmp_path: Path) -> None:
    env = _entrypoint_env(tmp_path, 'echo "migrate $*"; exit 4', "echo SHOULD-NOT-RUN")
    env["HIVEMIND_RUNNER"] = "migrate"
    proc = subprocess.run(
        ["sh", str(ROOT / "entrypoint.sh"), "--rollback", "1"],
        env=env,
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 4
    assert proc.stdout.split() == ["migrate", "--rollback", "1"]


def test_entrypoint_migrate_runner_sigterm_exits_promptly(tmp_path: Path) -> None:
    env = _entrypoint_env(tmp_path, "exec sleep 60", "echo SHOULD-NOT-RUN")
    env["HIVEMIND_RUNNER"] = "migrate"
    proc = subprocess.Popen(["sh", str(ROOT / "entrypoint.sh")], env=env, text=True)
    time.sleep(1.0)
    proc.send_signal(signal.SIGTERM)
    try:
        assert proc.wait(timeout=10) == 143
    finally:
        if proc.poll() is None:
            proc.kill()
