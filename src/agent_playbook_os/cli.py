from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import json
import sys
from uuid import uuid4

from . import __version__
from .catalog import PlaybookCatalog
from .compiler import Compiler
from .diffing import diff_runs
from .engine import Runner
from .evals import run_eval_suite
from .loader import load_playbook
from .locks import lock_from_plan
from .models import CompiledPlan, RunStatus, ProviderReceipt, PolicySpec
from .operator import DeterministicOperator
from .runtime import ReferenceRuntime
from .recording import RecordingRuntime, ReplayRuntime
from .telemetry import JsonlTelemetrySink, NullTelemetrySink
from .conformance import run_runtime_conformance
from .benchmark import benchmark_plan
from .attestation import create_attestation, verify_attestation
from .models import RunAttestation
from .skills import SkillResolver
from .store import RunStore
from .storage import SQLiteRunStore, open_run_store, migrate_run_state
from .secret_resolver import EnvSecretResolver, FileSecretResolver, SecretResolverChain
from .negotiation import negotiate_runtime
from .plugins import PluginRegistry
from .inspection import inspect_run
from .planning import (
    ModelBackedOperator, StaticPlannerRuntime, prepare_operator_execution, run_operator_goal,
    persist_operator_review_artifacts,
)
from .promotion import PromotionRegistry, load_candidate
from .lab import compare_runtimes
from .errors import PlanApprovalRequired
from .policy import assert_plan_satisfies_host_policy
from .distributed import (
    DistributedWorker, SQLiteFencingCoordinator, SQLiteWorkQueue, TenantScope,
    WorkSubmission, distributed_queue_conformance, required_capabilities_for_plan,
)
from .tenancy import TenantFileSecretResolver, TenantPolicyResolver
from .artifacts import ArtifactVerifierRegistry, FilesystemArtifactStore


def parse_inputs(items):
    result = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"input must be key=value: {item}")
        k, v = item.split("=", 1)
        try:
            result[k] = json.loads(v)
        except json.JSONDecodeError:
            result[k] = v
    return result


def load_mapping_file(path):
    import yaml
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    value = json.loads(raw) if p.suffix.lower() == ".json" else yaml.safe_load(raw)
    if not isinstance(value, dict):
        raise SystemExit(f"expected mapping in {path}")
    return value


def make_compiler(args):
    roots = getattr(args, "skills_root", None) or []
    resolver = SkillResolver(roots) if roots else None
    policy_path = getattr(args, "policy", None)
    host_policy = PolicySpec.model_validate(load_mapping_file(policy_path)) if policy_path else None
    return Compiler(
        resolver, strict_skill_resolution=getattr(args, "strict_skills", False), host_policy=host_policy
    )


def load_host_policy(args):
    policy_path = getattr(args, "policy", None)
    return PolicySpec.model_validate(load_mapping_file(policy_path)) if policy_path else None


def enforce_current_host_policy(plan, args):
    assert_plan_satisfies_host_policy(plan, load_host_policy(args))


def make_runtime(args):
    record = getattr(args, "record_cassette", None)
    replay = getattr(args, "replay_cassette", None)
    if record and replay:
        raise SystemExit("--record-cassette and --replay-cassette are mutually exclusive")
    if replay:
        return ReplayRuntime(replay)
    runtime = ReferenceRuntime(
        dry_run=getattr(args, "dry_run", False),
        allow_commands=getattr(args, "allow_commands", False),
        command_cwd=getattr(args, "cwd", None),
        allowed_commands=set(getattr(args, "allow_command", None) or []),
        max_command_output_bytes=getattr(args, "max_command_output_bytes", 1_000_000),
    )
    return RecordingRuntime(runtime, record) if record else runtime


def make_telemetry(args):
    path = getattr(args, "telemetry_jsonl", None)
    return JsonlTelemetrySink(path) if path else NullTelemetrySink()


def make_store_factory(args):
    backend = getattr(args, "store", "filesystem")
    return SQLiteRunStore if backend == "sqlite" else RunStore


def make_secret_resolver(args):
    resolvers = [EnvSecretResolver()]
    roots = getattr(args, "file_secret_root", None) or []
    if roots:
        resolvers.append(FileSecretResolver(roots))
    return SecretResolverChain(resolvers)


def cmd_validate(args):
    pb = load_playbook(args.playbook)
    make_compiler(args).compile(pb, parse_inputs(args.input), source_path=args.playbook)
    print(f"OK {pb.metadata.id}@{pb.metadata.version}")


def cmd_compile(args):
    pb = load_playbook(args.playbook)
    plan = make_compiler(args).compile(pb, parse_inputs(args.input), source_path=args.playbook)
    payload = plan.model_dump_json(indent=2, by_alias=True)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        print(args.out)
    else:
        print(payload)


def _run_dir(root, run_id):
    return Path(root).resolve() / run_id


async def cmd_run_async(args):
    pb = load_playbook(args.playbook)
    compiler = make_compiler(args)
    plan = compiler.compile(pb, parse_inputs(args.input), source_path=args.playbook)
    run_id = args.run_id or str(uuid4())
    run_dir = _run_dir(args.run_root, run_id)
    state = await Runner(
        make_runtime(args), compiler, telemetry=make_telemetry(args),
        secret_resolver=make_secret_resolver(args),
        store_factory=make_store_factory(args),
        lease_ttl_seconds=args.lease_ttl_seconds,
        lease_heartbeat_seconds=args.lease_heartbeat_seconds,
        enforce_capabilities=args.strict_capabilities,
    ).run(
        plan,
        run_dir,
        approvals=set(args.approve or []),
        approval_actor=args.approval_actor,
    )
    print(json.dumps({
        "run_id": state.run_id,
        "status": state.status.value,
        "run_dir": str(run_dir),
        "outputs": state.outputs,
        "usage": state.usage.model_dump(mode="json"),
    }, indent=2))
    return 0 if state.status in {RunStatus.COMPLETED, RunStatus.WAITING_APPROVAL} else 1


def cmd_run(args):
    raise SystemExit(asyncio.run(cmd_run_async(args)))


async def cmd_resume_async(args):
    store = open_run_store(args.run_dir)
    plan = store.load_plan()
    enforce_current_host_policy(plan, args)
    compiler = Compiler()
    state = await Runner(
        make_runtime(args), compiler, telemetry=make_telemetry(args),
        secret_resolver=make_secret_resolver(args),
        store_factory=type(store),
        lease_ttl_seconds=args.lease_ttl_seconds,
        lease_heartbeat_seconds=args.lease_heartbeat_seconds,
        enforce_capabilities=args.strict_capabilities,
    ).run(
        plan,
        args.run_dir,
        approvals=set(args.approve or []),
        resume=True,
        approval_actor=args.approval_actor,
        allow_runtime_change=args.allow_runtime_change,
    )
    print(json.dumps({
        "run_id": state.run_id,
        "status": state.status.value,
        "outputs": state.outputs,
        "usage": state.usage.model_dump(mode="json"),
    }, indent=2))
    return 0 if state.status in {RunStatus.COMPLETED, RunStatus.WAITING_APPROVAL} else 1


def cmd_resume(args):
    raise SystemExit(asyncio.run(cmd_resume_async(args)))


def cmd_status(args):
    state = open_run_store(args.run_dir).load_state()
    print(state.model_dump_json(indent=2))


def cmd_inspect(args):
    verifier, scope = _artifact_verifier_from_args(args)
    print(json.dumps(inspect_run(args.run_dir, artifact_verifier=verifier, tenant_scope=scope), indent=2))


def cmd_migrate_state(args):
    store = open_run_store(args.run_dir)
    print(json.dumps(migrate_run_state(store, actor=args.actor), indent=2))


def cmd_receipts(args):
    state = open_run_store(args.run_dir).load_state()
    rows = [x.model_dump(mode="json") for x in state.provider_receipts]
    print(json.dumps(rows, indent=2))


def cmd_receipt_attach(args):
    metadata = {}
    if args.metadata:
        try:
            metadata = json.loads(args.metadata)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--metadata must be valid JSON: {exc}")
        if not isinstance(metadata, dict):
            raise SystemExit("--metadata must decode to an object")
    receipt = ProviderReceipt(
        provider=args.provider,
        call_id=args.call_id,
        operation=args.operation,
        status=args.status,
        idempotency_key=args.idempotency_key,
        metadata=metadata,
    )
    state = open_run_store(args.run_dir).attach_provider_receipt(
        args.invocation_id, receipt, actor=args.actor
    )
    print(json.dumps({
        "receipt": receipt.model_dump(mode="json"),
        "run_id": state.run_id,
    }, indent=2))


def cmd_uncertain(args):
    from .models import InvocationStatus
    state = open_run_store(args.run_dir).load_state()
    rows = [
        x.model_dump(mode="json") for x in state.invocations
        if x.status in {InvocationStatus.STARTED, InvocationStatus.UNKNOWN}
    ]
    print(json.dumps(rows, indent=2))


def cmd_trace(args):
    events = open_run_store(args.run_dir).read_events()
    for event in events:
        if args.step and event.get("step_id") != args.step:
            continue
        if args.type and event.get("type") != args.type:
            continue
        print(json.dumps(event, ensure_ascii=False))


async def cmd_replay_async(args):
    old = open_run_store(args.run_dir)
    plan = old.load_plan()
    enforce_current_host_policy(plan, args)
    old_state = old.load_state()
    new_id = args.run_id or str(uuid4())
    new_dir = _run_dir(args.run_root, new_id)
    compiler = Compiler()
    state = await Runner(
        make_runtime(args), compiler, telemetry=make_telemetry(args),
        secret_resolver=make_secret_resolver(args),
        store_factory=make_store_factory(args),
        lease_ttl_seconds=args.lease_ttl_seconds,
        lease_heartbeat_seconds=args.lease_heartbeat_seconds,
        enforce_capabilities=args.strict_capabilities,
    ).run(
        plan,
        new_dir,
        approvals=set(args.approve or []),
        replayed_from=old_state.run_id,
        parent_run_id=old_state.run_id,
        root_run_id=old_state.root_run_id or old_state.run_id,
        fork_reason="replay",
        parent_lineage_depth=old_state.lineage_depth,
        approval_actor=args.approval_actor,
    )
    print(json.dumps({
        "replayed_from": old_state.run_id,
        "run_dir": str(new_dir),
        "run_id": state.run_id,
        "status": state.status.value,
        "usage": state.usage.model_dump(mode="json"),
    }, indent=2))


def cmd_replay(args):
    asyncio.run(cmd_replay_async(args))



async def cmd_fork_async(args):
    parent_store = open_run_store(args.run_dir)
    parent_state = parent_store.load_state()
    plan = (
        CompiledPlan.model_validate_json(Path(args.plan).read_text(encoding="utf-8"))
        if args.plan
        else parent_store.load_plan()
    )
    enforce_current_host_policy(plan, args)
    new_id = args.run_id or str(uuid4())
    new_dir = _run_dir(args.run_root, new_id)
    compiler = Compiler()
    state = await Runner(
        make_runtime(args), compiler, telemetry=make_telemetry(args),
        secret_resolver=make_secret_resolver(args), store_factory=make_store_factory(args),
        lease_ttl_seconds=args.lease_ttl_seconds,
        lease_heartbeat_seconds=args.lease_heartbeat_seconds,
        enforce_capabilities=args.strict_capabilities,
    ).run(
        plan, new_dir, approvals=set(args.approve or []), approval_actor=args.approval_actor,
        parent_run_id=parent_state.run_id, root_run_id=parent_state.root_run_id or parent_state.run_id,
        fork_reason=args.reason, parent_lineage_depth=parent_state.lineage_depth,
    )
    print(json.dumps({
        "forked_from": parent_state.run_id, "root_run_id": state.root_run_id,
        "lineage_depth": state.lineage_depth, "run_id": state.run_id,
        "run_dir": str(new_dir), "status": state.status.value,
    }, indent=2))


def cmd_fork(args):
    asyncio.run(cmd_fork_async(args))


def cmd_lineage(args):
    state = open_run_store(args.run_dir).load_state()
    print(json.dumps({
        "run_id": state.run_id,
        "parent_run_id": state.parent_run_id,
        "root_run_id": state.root_run_id,
        "lineage_depth": state.lineage_depth,
        "fork_reason": state.fork_reason,
        "replayed_from": state.replayed_from,
    }, indent=2))


def _load_mapping(path):
    return load_mapping_file(path)


async def cmd_operate_async(args):
    if args.planner_response:
        planner = StaticPlannerRuntime(_load_mapping(args.planner_response))
    else:
        config = {}
        if args.planner_config:
            try:
                config = json.loads(args.planner_config)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"--planner-config must be valid JSON: {exc}")
            if not isinstance(config, dict):
                raise SystemExit("--planner-config must decode to an object")
        planner = PluginRegistry().create("planner", args.planner_plugin, **config)
    operator = ModelBackedOperator(planner)
    catalog = PlaybookCatalog.from_roots(args.catalog_root)
    compiler = make_compiler(args)
    runtime = make_runtime(args)
    run_id = args.run_id or str(uuid4())
    run_dir = _run_dir(args.run_root, run_id)
    if args.prepare_only:
        prepared = await prepare_operator_execution(
            goal=args.goal, operator=operator, catalog=catalog, compiler=compiler, runtime=runtime,
            inputs=parse_inputs(args.input), context=_load_mapping(args.context) if args.context else None,
            ephemeral_path=run_dir / "operator" / "ephemeral-playbook.yaml",
            require_capabilities=args.strict_capabilities,
        )
        persist_operator_review_artifacts(prepared, run_dir)
        print(json.dumps({
            "prepared": True, "mode": prepared.decision.mode, "run_dir": str(run_dir),
            "playbook_id": prepared.plan.playbook_id, "plan_semantic_hash": prepared.plan.semantic_hash,
            "compiled_plan": str(run_dir / "operator" / "compiled-plan.json"),
            "gate": prepared.gate.model_dump(mode="json"),
        }, indent=2))
        return
    registry = PromotionRegistry(args.promotion_registry) if args.promotion_registry else None
    runner = Runner(
        runtime, compiler, telemetry=make_telemetry(args),
        secret_resolver=make_secret_resolver(args),
        store_factory=make_store_factory(args),
        lease_ttl_seconds=args.lease_ttl_seconds,
        lease_heartbeat_seconds=args.lease_heartbeat_seconds,
        enforce_capabilities=args.strict_capabilities,
    )
    try:
        prepared, state = await run_operator_goal(
            goal=args.goal, operator=operator, catalog=catalog, compiler=compiler, runtime=runtime,
            run_dir=run_dir, inputs=parse_inputs(args.input),
            context=_load_mapping(args.context) if args.context else None,
            approvals=set(args.approve or []), approval_actor=args.approval_actor,
            promotion_registry=registry, require_capabilities=args.strict_capabilities,
            runner=runner,
            approve_ephemeral_plan=args.approve_plan,
        )
    except PlanApprovalRequired as exc:
        print(json.dumps({
            "status": "PLAN_APPROVAL_REQUIRED",
            "run_dir": str(run_dir),
            "plan_semantic_hash": exc.plan_semantic_hash,
            "ephemeral_playbook": str(run_dir / "operator" / "ephemeral-playbook.yaml"),
            "compiled_plan": str(run_dir / "operator" / "compiled-plan.json"),
            "hint": "Review the generated playbook/compiled plan, then rerun with --approve-plan or execute the compiled plan with run-plan.",
        }, indent=2))
        return
    print(json.dumps({
        "run_id": state.run_id, "status": state.status.value, "run_dir": str(run_dir),
        "operator_mode": prepared.decision.mode, "playbook_id": prepared.plan.playbook_id,
        "plan_semantic_hash": prepared.plan.semantic_hash, "outputs": state.outputs,
    }, indent=2))


def cmd_operate(args):
    asyncio.run(cmd_operate_async(args))


async def cmd_run_plan_async(args):
    plan = CompiledPlan.model_validate_json(Path(args.plan).read_text(encoding="utf-8"))
    enforce_current_host_policy(plan, args)
    run_id = args.run_id or str(uuid4())
    run_dir = _run_dir(args.run_root, run_id)
    state = await Runner(
        make_runtime(args), Compiler(), telemetry=make_telemetry(args),
        secret_resolver=make_secret_resolver(args),
        store_factory=make_store_factory(args),
        lease_ttl_seconds=args.lease_ttl_seconds,
        lease_heartbeat_seconds=args.lease_heartbeat_seconds,
        enforce_capabilities=args.strict_capabilities,
    ).run(
        plan,
        run_dir,
        approvals=set(args.approve or []),
        approval_actor=args.approval_actor,
        run_metadata={"execution_source": "reviewed-compiled-plan"},
    )
    print(json.dumps({
        "run_id": state.run_id,
        "status": state.status.value,
        "run_dir": str(run_dir),
        "plan_semantic_hash": plan.semantic_hash,
        "outputs": state.outputs,
        "usage": state.usage.model_dump(mode="json"),
    }, indent=2))
    return 0 if state.status in {RunStatus.COMPLETED, RunStatus.WAITING_APPROVAL} else 1


def cmd_run_plan(args):
    raise SystemExit(asyncio.run(cmd_run_plan_async(args)))


def cmd_candidate_record(args):
    playbook = load_candidate(args.playbook)
    obs = PromotionRegistry(args.registry).record(playbook, args.run_dir, goal=args.goal)
    print(obs.model_dump_json(indent=2))


def cmd_candidate_status(args):
    playbook = load_candidate(args.playbook)
    summary = PromotionRegistry(args.registry).summary(
        playbook, min_successes=args.min_successes, min_distinct_goals=args.min_distinct_goals
    )
    print(summary.model_dump_json(indent=2))
    if args.require_ready and not summary.ready:
        raise SystemExit(1)


def cmd_candidate_verify(args):
    ok, error = PromotionRegistry(args.registry).verify()
    print(json.dumps({"ok": ok, "error": error}, indent=2))
    if not ok:
        raise SystemExit(1)


def cmd_candidate_promote(args):
    playbook = load_candidate(args.playbook)
    receipt = PromotionRegistry(args.registry).promote(
        playbook, args.destination, actor=args.actor, min_successes=args.min_successes,
        min_distinct_goals=args.min_distinct_goals, new_id=args.new_id, new_version=args.new_version, force=args.force
    )
    print(receipt.model_dump_json(indent=2))


async def cmd_lab_compare_async(args):
    pb = load_playbook(args.playbook)
    plan = make_compiler(args).compile(pb, parse_inputs(args.input), source_path=args.playbook)
    variants = {}
    for item in args.variant:
        if "=" not in item:
            raise SystemExit("--variant must be name=cassette.jsonl")
        name, cassette = item.split("=", 1)
        if name in variants:
            raise SystemExit(f"duplicate variant name: {name}")
        variants[name] = (lambda path=cassette: ReplayRuntime(path))
    report = await compare_runtimes(
        plan, variants, repeats=args.repeat, root=args.run_root, approvals=set(args.approve or [])
    )
    print(report.model_dump_json(indent=2))


def cmd_lab_compare(args):
    asyncio.run(cmd_lab_compare_async(args))


def cmd_lock(args):
    pb = load_playbook(args.playbook)
    compiler = make_compiler(args)
    plan = compiler.compile(pb, parse_inputs(args.input), source_path=args.playbook)
    lock = lock_from_plan(plan)
    out = Path(args.out) if args.out else Path(args.playbook).with_suffix(Path(args.playbook).suffix + ".lock.json")
    out.write_text(lock.model_dump_json(indent=2), encoding="utf-8")
    print(str(out))


def cmd_skills_verify_plan(args):
    plan = CompiledPlan.model_validate_json(Path(args.plan).read_text(encoding="utf-8"))
    resolver = SkillResolver(args.root)
    results = {}
    for sid, entry in sorted(plan.skill_lock.items()):
        ok, error = resolver.verify_lock(entry)
        results[sid] = {"ok": ok, "error": error}
    overall = bool(results) and all(x["ok"] for x in results.values()) if plan.skill_lock else True
    print(json.dumps({"ok": overall, "skills": results}, indent=2))
    if not overall:
        raise SystemExit(1)


def cmd_skills_index(args):
    resolver = SkillResolver(args.root)
    print(json.dumps([x.model_dump() for x in resolver.list()], indent=2))


def cmd_verify(args):
    store = open_run_store(args.run_dir)
    checks: dict[str, object] = {}

    ok, error = store.verify_event_chain()
    checks["event_chain"] = {"ok": ok, "error": error}

    plan_ok, plan_error = store.verify_plan_integrity()
    checks["plan_integrity"] = {"ok": plan_ok, "error": plan_error}

    state_ok, state_error = store.verify_state_integrity()
    checks["state_integrity"] = {"ok": state_ok, "error": state_error}

    cross_ok = False
    artifact_ok = False
    artifact_errors: list[str] = []
    try:
        state = store.load_state()
        plan = store.load_plan()
        cross_ok = (
            state.plan_hash == plan.integrity_hash
            and state.plan_semantic_hash == plan.semantic_hash
            and state.playbook_hash == plan.playbook_hash
        )
        events = store.read_events()
        run_ids = {x.get("run_id") for x in events}
        cross_ok = cross_ok and (not run_ids or run_ids == {state.run_id})
        verifier, scope = _artifact_verifier_from_args(args)
        artifact_ok, artifact_errors = store.verify_artifacts(state.artifacts, verifier, scope)
    except Exception as exc:
        checks["cross_refs"] = {"ok": False, "error": str(exc)}
    else:
        checks["cross_refs"] = {"ok": cross_ok, "error": None if cross_ok else "state/plan/event references differ"}
    checks["artifacts"] = {"ok": artifact_ok, "errors": artifact_errors}

    overall = all(bool(v.get("ok")) for v in checks.values() if isinstance(v, dict))
    print(json.dumps({"ok": overall, "checks": checks}, indent=2))
    if not overall:
        raise SystemExit(1)


def cmd_diff(args):
    print(diff_runs(args.before, args.after).model_dump_json(indent=2))




def cmd_catalog_lint(args):
    cat = PlaybookCatalog.from_roots(args.root)
    collisions = cat.collisions()
    payload = {k: [x.model_dump(mode="json") for x in v] for k, v in collisions.items()}
    ok = not collisions
    print(json.dumps({"ok": ok, "entries": len(cat.entries), "collisions": payload}, indent=2))
    if not ok:
        raise SystemExit(1)


def cmd_catalog_list(args):
    cat = PlaybookCatalog.from_roots(args.root)
    print(json.dumps([e.model_dump() for e in cat.entries], indent=2))


def cmd_catalog_search(args):
    cat = PlaybookCatalog.from_roots(args.root)
    print(json.dumps([e.model_dump() for e in cat.search(args.query, args.limit)], indent=2))


async def cmd_route_async(args):
    cat = PlaybookCatalog.from_roots(args.root)
    result = await DeterministicOperator().select(args.goal, cat)
    print(result.model_dump_json(indent=2))


def cmd_route(args):
    asyncio.run(cmd_route_async(args))


async def cmd_eval_async(args):
    result = await run_eval_suite(args.suite, run_root=args.run_root)
    print(result.model_dump_json(indent=2))
    if result.failed:
        raise SystemExit(1)


def cmd_eval(args):
    asyncio.run(cmd_eval_async(args))


def cmd_capabilities(args):
    registry = make_runtime(args).capabilities()
    print(json.dumps([x.model_dump(mode="json") for x in registry.list()], indent=2))


def cmd_doctor(args):
    import importlib.metadata

    print("Agent Playbook OS doctor")
    print(f"version={__version__}")
    print(f"python={sys.version.split()[0]}")
    for pkg in ["pydantic", "PyYAML", "jsonschema"]:
        try:
            print(f"{pkg}={importlib.metadata.version(pkg)}")
        except importlib.metadata.PackageNotFoundError:
            print(f"{pkg}=MISSING")


def _attestation_key(args) -> bytes:
    import os
    value = os.environ.get(args.key_env)
    if not value:
        raise SystemExit(f"attestation key environment variable is not set: {args.key_env}")
    return value.encode("utf-8")


def cmd_attest(args):
    verifier, scope = _artifact_verifier_from_args(args)
    att = create_attestation(
        args.run_dir, _attestation_key(args), signer=args.signer, artifact_verifier=verifier, tenant_scope=scope
    )
    out = Path(args.out) if args.out else Path(args.run_dir) / "attestation.json"
    out.write_text(att.model_dump_json(indent=2), encoding="utf-8")
    print(str(out))


def cmd_attestation_verify(args):
    att = RunAttestation.model_validate_json(Path(args.attestation).read_text(encoding="utf-8"))
    verifier, scope = _artifact_verifier_from_args(args)
    ok, error = verify_attestation(
        args.run_dir, att, _attestation_key(args), artifact_verifier=verifier, tenant_scope=scope
    )
    print(json.dumps({"ok": ok, "error": error}, indent=2))
    if not ok:
        raise SystemExit(1)


def cmd_cancel(args):
    payload = open_run_store(args.run_dir).request_cancellation(args.reason, args.actor)
    print(json.dumps(payload, indent=2))


def cmd_reconcile(args):
    output = None
    if args.output is not None:
        try:
            output = json.loads(args.output)
        except json.JSONDecodeError:
            output = args.output
    state = open_run_store(args.run_dir).reconcile_invocation(
        args.invocation_id,
        outcome=args.outcome,
        actor=args.actor,
        output=output,
        note=args.note,
    )
    print(state.model_dump_json(indent=2))


def cmd_lease_status(args):
    store = open_run_store(args.run_dir)
    payload = store.lease_status()
    print(json.dumps({"leased": payload is not None, "lease": payload}, indent=2))

def cmd_lease_break(args):
    payload = open_run_store(args.run_dir).break_lease(actor=args.actor)
    print(json.dumps({"broken": payload is not None, "lease": payload}, indent=2))


async def cmd_conformance_async(args):
    report = await run_runtime_conformance(make_runtime(args))
    print(report.model_dump_json(indent=2))
    if not report.passed:
        raise SystemExit(1)


def cmd_conformance(args):
    asyncio.run(cmd_conformance_async(args))


async def cmd_benchmark_async(args):
    pb = load_playbook(args.playbook)
    compiler = make_compiler(args)
    plan = compiler.compile(pb, parse_inputs(args.input), source_path=args.playbook)
    def factory():
        return ReferenceRuntime(
            dry_run=args.dry_run,
            allow_commands=args.allow_commands,
            command_cwd=args.cwd,
            allowed_commands=set(args.allow_command or []),
            max_command_output_bytes=args.max_command_output_bytes,
        )
    def runner_factory(runtime):
        return Runner(
            runtime, compiler, telemetry=make_telemetry(args),
            secret_resolver=make_secret_resolver(args),
            store_factory=make_store_factory(args),
            lease_ttl_seconds=args.lease_ttl_seconds,
            lease_heartbeat_seconds=args.lease_heartbeat_seconds,
            enforce_capabilities=args.strict_capabilities,
        )
    report = await benchmark_plan(
        plan,
        factory,
        repeats=args.repeat,
        root=args.run_root,
        approvals=set(args.approve or []),
        approval_actor=args.approval_actor,
        runner_factory=runner_factory,
    )
    print(report.model_dump_json(indent=2))
    if report.failed:
        raise SystemExit(1)


def cmd_benchmark(args):
    asyncio.run(cmd_benchmark_async(args))


def cmd_preflight(args):
    pb = load_playbook(args.playbook)
    plan = make_compiler(args).compile(pb, parse_inputs(args.input), source_path=args.playbook)
    report = negotiate_runtime(plan, make_runtime(args))
    print(report.model_dump_json(indent=2))
    if not report.passed:
        raise SystemExit(1)


def cmd_plugins_list(args):
    descriptors = PluginRegistry().descriptors(args.kind)
    print(json.dumps([x.__dict__ for x in descriptors], indent=2))


def _artifact_verifier_from_args(args):
    root = getattr(args, "artifact_root", None)
    if not root:
        return None, None
    tenant = getattr(args, "tenant", None)
    namespace = getattr(args, "namespace", None)
    if not tenant or not namespace:
        raise SystemExit("--artifact-root requires --tenant and --namespace")
    registry = ArtifactVerifierRegistry()
    registry.register(FilesystemArtifactStore(root))
    return registry, TenantScope(tenant_id=tenant, namespace=namespace)


def cmd_queue_enqueue(args):
    plan_path = Path(args.plan).resolve()
    plan = CompiledPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    run_id = args.run_id or str(uuid4())
    from .tenancy import tenant_path
    scope = TenantScope(tenant_id=args.tenant, namespace=args.namespace)
    run_dir = tenant_path(Path(args.run_root).resolve(), scope, run_id)
    metadata = {}
    if args.metadata:
        metadata = json.loads(args.metadata)
        if not isinstance(metadata, dict):
            raise SystemExit("--metadata must decode to an object")
    submission = WorkSubmission(
        work_id=args.work_id or str(uuid4()), queue_name=args.queue_name, tenant_id=args.tenant, namespace=args.namespace,
        run_id=run_id, plan_path=str(plan_path), run_dir=str(run_dir), store_backend=args.store,
        required_capabilities=sorted(set(required_capabilities_for_plan(plan)) | set(args.require_capability or [])),
        approvals=list(args.approve or []), approval_actor=args.approval_actor, max_attempts=args.max_attempts, metadata=metadata,
    )
    item = SQLiteWorkQueue(args.queue_db).enqueue(submission)
    print(item.model_dump_json(indent=2))


def cmd_queue_list(args):
    rows = SQLiteWorkQueue(args.queue_db).list(
        queue_name=args.queue_name, tenant_id=args.tenant, namespace=args.namespace, status=args.status, limit=args.limit
    )
    print(json.dumps([x.model_dump(mode="json") for x in rows], indent=2))


def cmd_queue_stats(args):
    print(SQLiteWorkQueue(args.queue_db).stats(args.queue_name).model_dump_json(indent=2))


def cmd_queue_cancel(args):
    print(SQLiteWorkQueue(args.queue_db).request_cancel(args.work_id).model_dump_json(indent=2))


def cmd_queue_conformance(args):
    import tempfile
    root = Path(tempfile.mkdtemp(prefix="apbos-queue-conformance-"))
    counter = {"n": 0}
    def factory():
        counter["n"] += 1
        return SQLiteWorkQueue(root / f"probe-{counter['n']}.sqlite3")
    report = distributed_queue_conformance(factory)
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


async def cmd_worker_once_async(args):
    queue = SQLiteWorkQueue(args.queue_db)
    runtime = make_runtime(args)
    base_policy = load_host_policy(args)
    tenant_policy = TenantPolicyResolver(args.tenant_policy_root) if args.tenant_policy_root else None

    def policy_resolver(item):
        if tenant_policy is None:
            return base_policy
        return tenant_policy.resolve(TenantScope(tenant_id=item.tenant_id, namespace=item.namespace), base_policy)

    def runner_factory(store_backend="filesystem", item=None):
        resolvers = [EnvSecretResolver()]
        if args.file_secret_root:
            resolvers.append(FileSecretResolver(args.file_secret_root))
        if args.tenant_secret_root and item is not None:
            resolvers.append(TenantFileSecretResolver(
                args.tenant_secret_root, TenantScope(tenant_id=item.tenant_id, namespace=item.namespace)
            ))
        store_factory = SQLiteRunStore if store_backend == "sqlite" else RunStore
        return Runner(
            runtime, Compiler(), telemetry=make_telemetry(args), secret_resolver=SecretResolverChain(resolvers),
            store_factory=store_factory, lease_ttl_seconds=args.lease_ttl_seconds,
            lease_heartbeat_seconds=args.lease_heartbeat_seconds, enforce_capabilities=args.strict_capabilities,
        )

    coordinator = None if args.no_fencing else SQLiteFencingCoordinator(args.fence_db or (str(args.queue_db) + ".fences.sqlite3"))
    worker = DistributedWorker(
        queue=queue, runtime=runtime, runner_factory=runner_factory, queue_name=args.queue_name, worker_id=args.worker_id,
        concurrency=args.concurrency, visibility_timeout_seconds=args.visibility_timeout_seconds,
        heartbeat_seconds=args.worker_heartbeat_seconds, tenant_id=args.tenant, namespace=args.namespace,
        retry_delay_seconds=args.retry_delay_seconds, strict_capabilities=True, policy_resolver=policy_resolver,
        coordinator=coordinator, fencing_ttl_seconds=args.fencing_ttl_seconds,
        allowed_run_root=args.allowed_run_root, allowed_plan_root=args.allowed_plan_root,
    )
    results = await worker.run_once()
    payload = [{
        "work": x.work.model_dump(mode="json"), "state_status": x.state_status, "error": x.error
    } for x in results]
    print(json.dumps(payload, indent=2))
    if any(x.error and x.work.status in {"DEAD", "FAILED"} for x in results):
        raise SystemExit(1)


def cmd_worker_once(args):
    asyncio.run(cmd_worker_once_async(args))


def add_common(p):
    p.add_argument("--input", action="append", default=[], help="key=value; JSON values supported")
    p.add_argument("--skills-root", action="append", default=[])
    p.add_argument("--strict-skills", action="store_true")
    p.add_argument("--policy", help="host/org policy overlay JSON/YAML; may only restrict playbook policy")


def add_policy(p):
    p.add_argument("--policy", help="current host/org policy; persisted plans must already satisfy it")


def add_artifact_verification(p):
    p.add_argument("--artifact-root", help="root for artifact+file:// external artifact verification")
    p.add_argument("--tenant", help="tenant scope for external artifacts")
    p.add_argument("--namespace", help="namespace scope for external artifacts")


def add_runtime(p):
    p.add_argument("--dry-run", action="store_true", help="preview skill/agent/command invocations; run state is still persisted")
    p.add_argument("--allow-commands", action="store_true")
    p.add_argument("--allow-command", action="append", default=[], help="required executable allowlist with --allow-commands; repeatable")
    p.add_argument("--max-command-output-bytes", type=int, default=1_000_000)
    p.add_argument("--cwd", default=None)
    p.add_argument("--approve", action="append", default=[])
    p.add_argument("--approval-actor", default="human")
    p.add_argument("--record-cassette")
    p.add_argument("--replay-cassette")
    p.add_argument("--telemetry-jsonl")
    p.add_argument("--store", choices=["filesystem", "sqlite"], default="filesystem")
    p.add_argument("--file-secret-root", action="append", default=[])
    p.add_argument("--lease-ttl-seconds", type=float, default=30.0)
    p.add_argument("--lease-heartbeat-seconds", type=float, default=None)
    p.add_argument("--allow-runtime-change", action="store_true")
    p.add_argument("--strict-capabilities", action="store_true", help="fail before execution when runtime capabilities do not satisfy the compiled plan")


def build_parser():
    parser = argparse.ArgumentParser(prog="playbook", description="Agent Playbook OS reference CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate")
    p.add_argument("playbook")
    add_common(p)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("compile")
    p.add_argument("playbook")
    p.add_argument("--out")
    add_common(p)
    p.set_defaults(func=cmd_compile)

    p = sub.add_parser("lock")
    p.add_argument("playbook")
    p.add_argument("--out")
    add_common(p)
    p.set_defaults(func=cmd_lock)

    p = sub.add_parser("run")
    p.add_argument("playbook")
    p.add_argument("--run-root", default=".playbook-runs")
    p.add_argument("--run-id")
    add_common(p)
    add_runtime(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("resume")
    p.add_argument("run_dir")
    add_runtime(p)
    add_policy(p)
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("status")
    p.add_argument("run_dir")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("inspect")
    p.add_argument("run_dir")
    add_artifact_verification(p)
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("migrate-state")
    p.add_argument("run_dir")
    p.add_argument("--actor", default="operator")
    p.set_defaults(func=cmd_migrate_state)

    p = sub.add_parser("receipts")
    p.add_argument("run_dir")
    p.set_defaults(func=cmd_receipts)

    p = sub.add_parser("receipt")
    rs = p.add_subparsers(dest="receipt_command", required=True)
    q = rs.add_parser("attach")
    q.add_argument("run_dir")
    q.add_argument("invocation_id")
    q.add_argument("--provider", required=True)
    q.add_argument("--call-id", required=True)
    q.add_argument("--operation")
    q.add_argument("--status", choices=["accepted", "completed", "unknown", "failed"], default="accepted")
    q.add_argument("--idempotency-key")
    q.add_argument("--metadata")
    q.add_argument("--actor", default="operator")
    q.set_defaults(func=cmd_receipt_attach)

    p = sub.add_parser("uncertain")
    p.add_argument("run_dir")
    p.set_defaults(func=cmd_uncertain)

    p = sub.add_parser("trace")
    p.add_argument("run_dir")
    p.add_argument("--step")
    p.add_argument("--type")
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("replay")
    p.add_argument("run_dir")
    p.add_argument("--run-root", default=".playbook-runs")
    p.add_argument("--run-id")
    add_runtime(p)
    add_policy(p)
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("fork")
    p.add_argument("run_dir")
    p.add_argument("--run-root", default=".playbook-runs")
    p.add_argument("--run-id")
    p.add_argument("--plan", help="optional alternate compiled plan JSON")
    p.add_argument("--reason", default="manual-fork")
    add_runtime(p)
    add_policy(p)
    p.set_defaults(func=cmd_fork)

    p = sub.add_parser("lineage")
    p.add_argument("run_dir")
    p.set_defaults(func=cmd_lineage)

    p = sub.add_parser("verify")
    p.add_argument("run_dir")
    add_artifact_verification(p)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("diff")
    p.add_argument("before")
    p.add_argument("after")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("eval")
    p.add_argument("suite")
    p.add_argument("--run-root")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("catalog")
    cs = p.add_subparsers(dest="catalog_command", required=True)
    q = cs.add_parser("list")
    q.add_argument("root", nargs="+", default=["playbooks"])
    q.set_defaults(func=cmd_catalog_list)
    q = cs.add_parser("search")
    q.add_argument("query")
    q.add_argument("--root", action="append", default=["playbooks"])
    q.add_argument("--limit", type=int, default=10)
    q.set_defaults(func=cmd_catalog_search)
    q = cs.add_parser("lint")
    q.add_argument("root", nargs="+", default=["playbooks"])
    q.set_defaults(func=cmd_catalog_lint)

    p = sub.add_parser("route")
    p.add_argument("goal")
    p.add_argument("--root", action="append", default=["playbooks"])
    p.set_defaults(func=cmd_route)

    p = sub.add_parser("operate")
    p.add_argument("goal")
    planner_group = p.add_mutually_exclusive_group(required=True)
    planner_group.add_argument("--planner-response", help="JSON/YAML response from a host model planner")
    planner_group.add_argument("--planner-plugin", help="explicitly load a registered planner plugin")
    p.add_argument("--planner-config", help="non-secret JSON object passed to planner plugin factory")
    p.add_argument("--catalog-root", action="append", default=["playbooks"])
    p.add_argument("--context", help="optional JSON/YAML operator context")
    p.add_argument("--prepare-only", action="store_true")
    p.add_argument("--approve-plan", action="store_true", help="approve execution of an Operator-generated ephemeral plan after review")
    p.add_argument("--promotion-registry")
    p.add_argument("--run-root", default=".playbook-runs")
    p.add_argument("--run-id")
    add_common(p)
    add_runtime(p)
    p.set_defaults(func=cmd_operate)

    p = sub.add_parser("run-plan")
    p.add_argument("plan", help="compiled plan JSON previously inspected/reviewed")
    p.add_argument("--run-root", default=".playbook-runs")
    p.add_argument("--run-id")
    add_runtime(p)
    add_policy(p)
    p.set_defaults(func=cmd_run_plan)

    p = sub.add_parser("candidate")
    cs = p.add_subparsers(dest="candidate_command", required=True)
    q = cs.add_parser("record")
    q.add_argument("playbook")
    q.add_argument("run_dir")
    q.add_argument("--registry", default=".playbook-os/promotion.jsonl")
    q.add_argument("--goal")
    q.set_defaults(func=cmd_candidate_record)
    q = cs.add_parser("status")
    q.add_argument("playbook")
    q.add_argument("--registry", default=".playbook-os/promotion.jsonl")
    q.add_argument("--min-successes", type=int, default=3)
    q.add_argument("--min-distinct-goals", type=int, default=1)
    q.add_argument("--require-ready", action="store_true")
    q.set_defaults(func=cmd_candidate_status)
    q = cs.add_parser("verify")
    q.add_argument("--registry", default=".playbook-os/promotion.jsonl")
    q.set_defaults(func=cmd_candidate_verify)
    q = cs.add_parser("promote")
    q.add_argument("playbook")
    q.add_argument("destination")
    q.add_argument("--registry", default=".playbook-os/promotion.jsonl")
    q.add_argument("--min-successes", type=int, default=3)
    q.add_argument("--min-distinct-goals", type=int, default=1)
    q.add_argument("--actor", default="human")
    q.add_argument("--id", dest="new_id")
    q.add_argument("--version", dest="new_version")
    q.add_argument("--force", action="store_true")
    q.set_defaults(func=cmd_candidate_promote)

    p = sub.add_parser("lab")
    ls = p.add_subparsers(dest="lab_command", required=True)
    q = ls.add_parser("compare")
    q.add_argument("playbook")
    q.add_argument("--variant", action="append", required=True, help="name=cassette.jsonl; repeatable")
    q.add_argument("--repeat", type=int, default=1)
    q.add_argument("--run-root")
    add_common(q)
    q.add_argument("--approve", action="append", default=[])
    q.set_defaults(func=cmd_lab_compare)

    p = sub.add_parser("attest")
    p.add_argument("run_dir")
    p.add_argument("--out")
    p.add_argument("--signer", default="local-hmac")
    p.add_argument("--key-env", default="APBOS_ATTEST_KEY")
    add_artifact_verification(p)
    p.set_defaults(func=cmd_attest)

    p = sub.add_parser("attestation")
    ats = p.add_subparsers(dest="attestation_command", required=True)
    q = ats.add_parser("verify")
    q.add_argument("run_dir")
    q.add_argument("attestation")
    q.add_argument("--key-env", default="APBOS_ATTEST_KEY")
    add_artifact_verification(q)
    q.set_defaults(func=cmd_attestation_verify)

    p = sub.add_parser("cancel")
    p.add_argument("run_dir")
    p.add_argument("--reason", default="requested")
    p.add_argument("--actor", default="human")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("reconcile")
    p.add_argument("run_dir")
    p.add_argument("invocation_id")
    p.add_argument("--outcome", choices=["not-executed", "succeeded"], required=True)
    p.add_argument("--output")
    p.add_argument("--note")
    p.add_argument("--actor", default="human")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("lease")
    ls = p.add_subparsers(dest="lease_command", required=True)
    q = ls.add_parser("status")
    q.add_argument("run_dir")
    q.set_defaults(func=cmd_lease_status)
    q = ls.add_parser("break")
    q.add_argument("run_dir")
    q.add_argument("--actor", default="human")
    q.set_defaults(func=cmd_lease_break)

    p = sub.add_parser("conformance")
    add_runtime(p)
    p.set_defaults(func=cmd_conformance)

    p = sub.add_parser("benchmark")
    p.add_argument("playbook")
    p.add_argument("--repeat", type=int, default=5)
    p.add_argument("--run-root")
    add_common(p)
    add_runtime(p)
    p.set_defaults(func=cmd_benchmark)

    p = sub.add_parser("preflight")
    p.add_argument("playbook")
    add_common(p)
    add_runtime(p)
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("plugins")
    ps = p.add_subparsers(dest="plugins_command", required=True)
    q = ps.add_parser("list")
    q.add_argument("--kind", choices=["runtime", "store", "telemetry", "secrets", "schema_resolver", "planner", "queue", "fencing", "artifact_store", "tenant_policy"])
    q.set_defaults(func=cmd_plugins_list)

    p = sub.add_parser("capabilities")
    add_runtime(p)
    p.set_defaults(func=cmd_capabilities)

    p = sub.add_parser("queue")
    qs = p.add_subparsers(dest="queue_command", required=True)
    q = qs.add_parser("enqueue")
    q.add_argument("plan", help="compiled plan JSON")
    q.add_argument("--queue-db", required=True)
    q.add_argument("--queue-name", default="default")
    q.add_argument("--tenant", default="default")
    q.add_argument("--namespace", default="default")
    q.add_argument("--run-root", default=".playbook-runs")
    q.add_argument("--run-id")
    q.add_argument("--work-id")
    q.add_argument("--store", choices=["filesystem", "sqlite"], default="filesystem")
    q.add_argument("--require-capability", action="append", default=[])
    q.add_argument("--approve", action="append", default=[])
    q.add_argument("--approval-actor", default="worker")
    q.add_argument("--max-attempts", type=int, default=3)
    q.add_argument("--metadata", help="JSON object")
    q.set_defaults(func=cmd_queue_enqueue)
    q = qs.add_parser("list")
    q.add_argument("--queue-db", required=True)
    q.add_argument("--queue-name")
    q.add_argument("--tenant")
    q.add_argument("--namespace")
    q.add_argument("--status")
    q.add_argument("--limit", type=int, default=100)
    q.set_defaults(func=cmd_queue_list)
    q = qs.add_parser("stats")
    q.add_argument("--queue-db", required=True)
    q.add_argument("--queue-name", default="default")
    q.set_defaults(func=cmd_queue_stats)
    q = qs.add_parser("cancel")
    q.add_argument("work_id")
    q.add_argument("--queue-db", required=True)
    q.set_defaults(func=cmd_queue_cancel)
    q = qs.add_parser("conformance")
    q.set_defaults(func=cmd_queue_conformance)

    p = sub.add_parser("worker")
    ws = p.add_subparsers(dest="worker_command", required=True)
    q = ws.add_parser("once")
    q.add_argument("--queue-db", required=True)
    q.add_argument("--queue-name", default="default")
    q.add_argument("--worker-id")
    q.add_argument("--concurrency", type=int, default=1)
    q.add_argument("--visibility-timeout-seconds", type=float, default=30.0)
    q.add_argument("--worker-heartbeat-seconds", type=float)
    q.add_argument("--retry-delay-seconds", type=float, default=0.0)
    q.add_argument("--tenant")
    q.add_argument("--namespace")
    q.add_argument("--tenant-policy-root")
    q.add_argument("--tenant-secret-root")
    q.add_argument("--fence-db")
    q.add_argument("--fencing-ttl-seconds", type=float)
    q.add_argument("--no-fencing", action="store_true")
    q.add_argument("--allowed-run-root", help="fail closed when queued run_dir escapes this root")
    q.add_argument("--allowed-plan-root", help="fail closed when queued plan_path escapes this root")
    add_runtime(q)
    add_policy(q)
    q.set_defaults(func=cmd_worker_once)

    p = sub.add_parser("doctor")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("skills")
    ss = p.add_subparsers(dest="skills_command", required=True)
    q = ss.add_parser("index")
    q.add_argument("root", nargs="+")
    q.set_defaults(func=cmd_skills_index)
    q = ss.add_parser("verify-plan")
    q.add_argument("plan")
    q.add_argument("root", nargs="+")
    q.set_defaults(func=cmd_skills_verify_plan)

    return parser


def main():
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    main()
