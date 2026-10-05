"""Set reconciliation and remaining-risk gates for the historical overlay.

The fixed IDs and provenance fingerprints come from the approved input ledger,
not the production implementation or this overlay's own declared counts.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

import pytest

LEDGER_PATH = Path(__file__).parents[1] / "docs/quality/historical-findings.json"
BASE = "18ee8ab82f22ea03ff1a1eb04b2edfb52ab3e93c"
TREE = "71d681b8a350b8f33c21d5867e8e17289da6131f"
INPUT_HASH = "d0468896ccc6188e1ae5d2fcde8cd396a52868abc191a57e311139ed09bcde18"
REPORT_HASH = "ed535d1fc83b0f53272167ab333048dfd73c043fe008c10b76819c49e2d9f64f"
HISTORICAL_METADATA_HASH = "53090aded0580060c572d6a902f5d3741059602aa9a2f09491e2753153dcc589"
SOURCE_METADATA_HASH = "ee558eb6af081a6142ee6f7f3ded7ab93ae07fe541eeb50caff57735162c286e"
RESIDUAL_SOURCE = "BETA2-INDEPENDENT-2026-10-05"
RESIDUAL_REPORT_HASH = "93fca3fcc04ae747d2d1078f6493659acfda45dc22cc945be5749dd3cc6d3ee6"
OLD_REVIEWED_HEAD = "a76bb1a228ed9c417a9444820f28e65cfc759938"
OLD_REVIEWED_TREE = "ae8f9fcc60affadae98d036de617dd52ed320dac"
# Complete 77-row overlay at the independently reviewed old HEAD, including
# implementation/review/closure fields. Advice is not permission to mutate it.
OLD_REVIEWED_ROWS_HASH = "0f16bd6ce51b8ccdb532373813583e1745c7c1b78d7bf8c614af2fbb0bbe389d"
EXPECTED_IDS = frozenset("""
R1-A1 R1-A2 R1-A3 R1-A4 R1-A5 R1-A6
R1-B1 R1-B2 R1-B3 R1-B4 R1-B5 R1-B6 R1-C1 R1-C2 R1-C3 R1-C4
R2-P1 R2-P2 R2-P3 R2-P4 R2-B1 R2-B2 R2-B3 R2-B4
R2-C1 R2-C2 R2-C3 R2-C5
M0-B1 M0-B2 M0-B3 M0-B4 M0-B5 M0-B6 M0-B7
M1-A1 M1-A2 M1-B1 M1-B2 M1-B3 M1-B4 M1-B5 M1-B6
M2-A1 M2-B1 M2-B2 M2-B3 M2-B4 M2-B5 M2-B6
M3-A1 M3-A2 M3-B1 M3-B2 M3R-B1 M3R-B2
M4-A1 M4-B1 M4-B2 M4-A2 M4-B3 M4-A3
M6-PRE-TOU M6-PRE-TAIL M6-PRE-UTC M6-A1 M6-B1
M7-A1 M7-B1 M7-B2 M7-U5 P10-A1 GATE-U1 GATE-U2 GATE-U3 BG-U1 BG-U2
""".split())
ACTIVE_CLASSES = {"PARTIALLY CLOSED", "STILL OPEN", "UNVERIFIED"}
CLASS_COUNTS = {
    "CLOSED": 30,
    "SUPERSEDED": 20,
    "PARTIALLY CLOSED": 9,
    "STILL OPEN": 13,
    "NOT APPLICABLE": 2,
    "UNVERIFIED": 3,
}
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
REVIEW_BINDING_POLICY = "ancestor_with_reviewed_tree"
REPOSITORY_PATH = LEDGER_PATH.parents[2]
# Registration authority: IR-B1-01_Second_Pass_Closure_Advice_2026-10-06.json,
# SHA256 5c099ece8925ef115b907462c98a6a4a5428ca12c52329f1788ea9e2fedd62b6.
# Only reference/hash identities are public; private audit payloads stay outside Git.
REVIEWED_HEAD = "46083af1c250f8e0f54c30af233f628159640b49"
REVIEWED_TREE = "03444b7b8c86209bf04f4de161bc3d69106a27c7"
IMPLEMENTATION_SNAPSHOT_HASH = "40470e368751136f401c7fc8776e799731187560e28448b34dbdf3d6cabf6da2"
REGISTRATION_REVIEWER = "Codex independent second-pass review Work; this session"
APPROVED_HISTORICAL_CLOSURES = frozenset(
    "M0-B1 M0-B6 M1-B2 M1-B3 M1-B4 M2-B2 M2-B3 M2-B4 M7-U5 R1-B4".split()
)
APPROVED_ADDITIONS = frozenset({"IR-B1-01", "IR-B1-03"})
APPROVED_CLOSURES = APPROVED_HISTORICAL_CLOSURES | APPROVED_ADDITIONS
REGISTRATION_REVISION = "FINAL-independent-review-closure-registration"
EXPECTED_REMAINING = frozenset("""
GATE-U1 GATE-U2 GATE-U3 M1-B5 M2-B5 M3-B1 M3R-B1 M3R-B2 M4-B2 M4-B3
R1-B2 R1-C4 R2-B3 R2-B4 R2-C3
""".split())
REGISTRATION_EVIDENCE = [
    {"ref": "previous/PR13_Residual_Closure_Advice_2026-10-05.json",
     "sha256": "47c49a32e530247cf46558ba6ea0a2f083fbd2522e308980124d096cfab5e466"},
    {"ref": "previous/Beta2_Independent_Closure_Recommendations_2026-10-05.json",
     "sha256": "9828c6c0be637e6bee93890007fcc0a1d5d26f97e6ceea77a028b332837b8e99"},
    {"ref": "evidence/second-pass-conclusion.json",
     "sha256": "b1df1c546c930b5286b602dca70ca5db6c6794c537182d0f2447eaf24cb55e44"},
    {"ref": "evidence/scope-ledger-privacy.json",
     "sha256": "775d34824df5e81eabb0efcdce58405d5c7c82768205801a824b45adcc98a43a"},
    {"ref": "IR-B1-01_Second_Pass_Review_2026-10-06.txt",
     "sha256": "334ee4baa09170da9a8641d23b04230e7fa614ec8814820598df322900fc0b90"},
    {"ref": "evidence/end-summary.json",
     "sha256": "b060337351a269b1120c0c620f92de2f7f5582b300f3ce096ea85f6493582342"},
]


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_evidence(evidence: object, *, required: bool, label: str) -> None:
    require(isinstance(evidence, list), f"{label}: evidence must be a list")
    require(not required or bool(evidence), f"{label}: missing evidence")
    for item in evidence:
        require(isinstance(item, dict), f"{label}: evidence needs a reference/hash pair")
        require(isinstance(item.get("ref"), str) and bool(item["ref"].strip()), f"{label}: missing evidence reference")
        require(isinstance(item.get("sha256"), str) and bool(HEX64.fullmatch(item["sha256"])), f"{label}: missing evidence hash")


def git_output(repository: Path, *arguments: str) -> str:
    """Read actual objects; never fetch or accept missing shallow history."""
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True, text=True, timeout=15, check=False,
    )
    require(result.returncode == 0, "review binding Git object unavailable; provide required history")
    return result.stdout.strip()


def verify_closed_git_binding(repository: Path, candidate: str, reviewed: str, reviewed_tree: str) -> None:
    """Bind closure to the reviewed commit/tree and real implementation ancestry."""
    for commit in (candidate, reviewed):
        require(git_output(repository, "cat-file", "-t", commit) == "commit", "review binding object is not a commit")
    require(
        git_output(repository, "rev-parse", "--verify", f"{reviewed}^{{tree}}") == reviewed_tree,
        "reviewed tree does not match actual Git tree",
    )
    current_head = git_output(repository, "rev-parse", "--verify", "HEAD")
    require(git_output(repository, "cat-file", "-t", current_head) == "commit", "ledger checkout HEAD is not a commit")
    for ancestor, descendant, message in (
        (candidate, reviewed, "implementation candidate is not an ancestor of reviewed HEAD"),
        (reviewed, current_head, "reviewed HEAD is not an ancestor of ledger checkout"),
    ):
        result = subprocess.run(
            ["git", "-C", str(repository), "merge-base", "--is-ancestor", ancestor, descendant],
            capture_output=True, text=True, timeout=15, check=False,
        )
        require(result.returncode in (0, 1), "review binding Git ancestry unavailable; provide required history")
        require(result.returncode == 0, message)


def reconcile_ledger(ledger: dict, *, repository: Path = REPOSITORY_PATH) -> dict:
    """Reject lost provenance or unsupported closure; report set and risk separately."""
    require(ledger.get("schema_version") == 1, "unknown ledger schema")
    require(ledger.get("review_binding_policy") == REVIEW_BINDING_POLICY, "unknown review binding policy")
    baseline = ledger["baseline"]
    require(baseline["commit"] == BASE and baseline["tree"] == TREE, "baseline object drift")
    require(baseline["input_ledger_sha256"] == INPUT_HASH, "input ledger hash drift")
    records = ledger["findings"]
    historical_ids = [record["finding_id"] for record in records]
    require(len(historical_ids) == len(set(historical_ids)), "duplicate historical finding")
    require(set(historical_ids) == EXPECTED_IDS, "historical set difference")
    require(baseline["source_record_count"] == 77, "incorrect input record count")

    sources = ledger["sources"]
    source_ids = [source["report_id"] for source in sources]
    require(len(source_ids) == len(set(source_ids)), "duplicate source report")
    source_map = {source["report_id"]: source["sha256"] for source in sources}
    require(all(HEX64.fullmatch(value) for value in source_map.values()), "invalid source hash")
    require(source_map.get("BETA1-HISTORICAL") == REPORT_HASH, "historical report hash drift")
    source_metadata = sorted((key, value) for key, value in source_map.items()
                             if key not in {"BETA1-HISTORICAL", RESIDUAL_SOURCE})
    require(fingerprint(source_metadata) == SOURCE_METADATA_HASH, "source provenance drift")
    require(source_map.get(RESIDUAL_SOURCE) == RESIDUAL_REPORT_HASH, "residual source provenance drift")

    additions = ledger["additional_findings"]
    additional_ids = [record["finding_id"] for record in additions]
    require(len(additional_ids) == len(set(additional_ids)), "duplicate additional finding")
    require(not set(additional_ids) & EXPECTED_IDS, "additional finding replaces historical finding")
    all_ids = set(historical_ids) | set(additional_ids)
    rows = []
    for record in records:
        historical_closure = record["historical_closure"]
        rows.append((record["finding_id"], record["original_severity"], record["beta1_classification"], record["related_findings"], record["source_report_id"], historical_closure["commits"], historical_closure["pull_requests"]))
        require(historical_closure["applies_to_commit"] == BASE, "historical closure must remain bound to beta1")
        require(historical_closure["classification"] == record["beta1_classification"], "historical classification mismatch")
        require(historical_closure["evidence"] == {"report_id": "BETA1-HISTORICAL", "sha256": REPORT_HASH}, "historical closure evidence drift")
        require(all(HEX40.fullmatch(commit) for commit in historical_closure["commits"]), "invalid historical closure commit")
    require(fingerprint(sorted(rows)) == HISTORICAL_METADATA_HASH, "historical metadata drift")
    require(Counter(record["beta1_classification"] for record in records) == CLASS_COUNTS, "historical classification count drift")

    active = [record for record in records if record["beta1_classification"] in ACTIVE_CLASSES]
    require(len(active) == baseline["active_source_record_count"] == 25, "active source count drift")
    closed_bindings = []
    for record in records + additions:
        finding_id = record["finding_id"]
        require(record["source_report_id"] in source_map, f"{finding_id}: unresolved source")
        require(all(related in all_ids for related in record["related_findings"]), f"{finding_id}: unresolved relation")
        require(bool(record["current_priority"]), f"{finding_id}: missing current priority")
        require(bool(record["disposition"]), f"{finding_id}: missing disposition")
        require(bool(record["owner_role"]), f"{finding_id}: missing owner")
        require(bool(record["acceptance"]["criteria"]) and all(isinstance(item, str) and item.strip() for item in record["acceptance"]["criteria"]), f"{finding_id}: missing acceptance contract")
        require(bool(record["next_review"]["checkpoints"]) and bool(record["next_review"]["triggers"]), f"{finding_id}: missing review conditions")

        implementation = record["implementation"]
        require(implementation["status"] in {"NOT_STARTED", "IMPLEMENTED_PENDING_REVIEW", "NOT_APPLICABLE_TO_IMPLEMENTATION"}, f"{finding_id}: implementation cannot independently close")
        implemented = implementation["status"] == "IMPLEMENTED_PENDING_REVIEW"
        require(not implemented or bool(HEX40.fullmatch(implementation["candidate_commit"] or "")), f"{finding_id}: missing candidate SHA")
        validate_evidence(record["acceptance"]["evidence"], required=implemented, label=f"{finding_id} acceptance")
        require(implementation["pull_request"] is None or (isinstance(implementation["pull_request"], int) and not isinstance(implementation["pull_request"], bool) and implementation["pull_request"] > 0), f"{finding_id}: invalid PR identity")

        review = record["independent_review"]
        require(review["status"] in {"NOT_PERFORMED", "PENDING", "PASSED", "FAILED"}, f"{finding_id}: invalid independent review state")
        reviewed = review["status"] in {"PASSED", "FAILED"}
        require(not reviewed or (bool(review["reviewer"]) and bool(HEX40.fullmatch(review["reviewed_head"] or ""))), f"{finding_id}: incomplete independent review")
        require(not reviewed or (isinstance(review.get("reviewed_tree"), str) and bool(HEX40.fullmatch(review["reviewed_tree"]))), f"{finding_id}: missing reviewed tree")
        if not reviewed:
            require(review["reviewer"] is None and review["reviewed_head"] is None and review.get("reviewed_tree") is None and not review["evidence"], f"{finding_id}: fabricated unperformed review")
        validate_evidence(review["evidence"], required=reviewed, label=f"{finding_id} review")

        closure = record["independent_closure"]
        require(closure["status"] in {"NOT_CLOSED", "CLOSED"}, f"{finding_id}: invalid closure state")
        closed = closure["status"] == "CLOSED"
        if closed:
            require(review["status"] == "PASSED" and implemented, f"{finding_id}: closure requires reviewed implementation")
            require(closure["closed_commit"] == review["reviewed_head"] and closure["reviewer"] == review["reviewer"], f"{finding_id}: closure identity mismatch")
        else:
            require(closure["closed_commit"] is None and closure["reviewer"] is None and not closure["evidence"], f"{finding_id}: incomplete closure fields")
        validate_evidence(closure["evidence"], required=closed, label=f"{finding_id} closure")
        if closed:
            review_pairs = {(item["ref"], item["sha256"]) for item in review["evidence"]}
            closure_pairs = {(item["ref"], item["sha256"]) for item in closure["evidence"]}
            require(bool(review_pairs & closure_pairs), f"{finding_id}: closure evidence is not bound to review evidence")
            closed_bindings.append((implementation["candidate_commit"], review["reviewed_head"], review["reviewed_tree"]))

        acceptance = record["risk_acceptance"]
        require(acceptance["decision"] in {None, "ACCEPTED", "REJECTED"}, f"{finding_id}: invalid risk acceptance")
        decided = acceptance["decision"] is not None
        require(not decided or (acceptance["owner"] and acceptance["reason"] and acceptance["expires_at"] and acceptance["review_triggers"]), f"{finding_id}: incomplete acceptance decision")
        if not decided:
            require(acceptance["owner"] is None and acceptance["reason"] is None and acceptance["expires_at"] is None and not acceptance["review_triggers"] and not acceptance["evidence"], f"{finding_id}: fabricated acceptance decision")
        validate_evidence(acceptance["evidence"], required=decided, label=f"{finding_id} risk acceptance")

    # An unclosed ledger needs no historical Git objects, including shallow CI.
    # Validate after every structural check so malformed evidence is diagnosed
    # before a missing object. Each final binding is verified once per group.
    for binding in sorted(set(closed_bindings)):
        verify_closed_git_binding(repository, *binding)

    remaining = [record for record in active if record["independent_closure"]["status"] != "CLOSED"]
    return {
        "set_completeness": {"input_count": 77, "located_count": len(records), "missing": [], "unexpected_historical": [], "additional_ids": sorted(additional_ids)},
        "remaining_risk": {"historically_active_count": len(active), "not_independently_closed_count": len(remaining), "finding_ids": sorted(record["finding_id"] for record in remaining), "additional_finding_ids": sorted(record["finding_id"] for record in additions), "additional_not_closed_count": sum(record["independent_closure"]["status"] != "CLOSED" for record in additions), "implementation_pending_review_count": sum(record["implementation"]["status"] == "IMPLEMENTED_PENDING_REVIEW" for record in active), "validation_boundaries": ledger["remaining_validation"]},
    }


def validate_residual_round_snapshot(ledger: dict) -> None:
    """Retained control for the historical, unregistered implementation phase."""
    require(fingerprint(ledger["findings"]) == OLD_REVIEWED_ROWS_HASH,
            "old reviewed historical overlay changed during residual implementation")
    additions = ledger["additional_findings"]
    require({row["finding_id"] for row in additions} == {"IR-B1-01", "IR-B1-03"}
            and len(additions) == 2, "residual scope differs from approved two observations")
    for row in additions:
        require(row["source_report_id"] == RESIDUAL_SOURCE
                and row["source_observation"]["observed_head"] == OLD_REVIEWED_HEAD
                and row["source_observation"]["observed_tree"] == OLD_REVIEWED_TREE
                and row["source_observation"]["report_sha256"] == RESIDUAL_REPORT_HASH,
                "residual observation is not bound to original independent source")
        require(row["original_severity"] == "B", "residual severity drift")
        require(row["implementation"]["status"] == "IMPLEMENTED_PENDING_REVIEW"
                and row["independent_review"]["status"] == "NOT_PERFORMED"
                and row["independent_closure"]["status"] == "NOT_CLOSED"
                and row["risk_acceptance"]["decision"] is None,
                "residual implementation cannot claim review, closure or accepted risk")


def test_residual_snapshot_preserves_history_and_unperformed_review(ledger):
    validate_residual_round_snapshot(ledger)


@pytest.mark.parametrize("mutation,message", [
    ("history", "historical overlay changed"), ("omitted", "approved two observations"),
    ("source", "bound to original independent source"), ("severity", "severity drift"),
    ("review", "cannot claim review"), ("closure", "cannot claim review"),
    ("risk", "cannot claim review"), ("implementation", "cannot claim review"),
])
def test_residual_gate_rejects_scope_drift_and_fabricated_decisions(ledger, mutation, message):
    row = ledger["additional_findings"][0]
    if mutation == "history":
        ledger["findings"][0]["independent_review"]["status"] = "PASSED"
    elif mutation == "omitted":
        ledger["additional_findings"].pop()
    elif mutation == "source":
        row["source_observation"]["observed_head"] = BASE
    elif mutation == "severity":
        row["original_severity"] = "A"
    else:
        field, value = {"review": ("independent_review", "PASSED"),
                        "closure": ("independent_closure", "CLOSED"),
                        "risk": ("risk_acceptance", "ACCEPTED"),
                        "implementation": ("implementation", "CLOSED")}[mutation]
        row[field]["decision" if mutation == "risk" else "status"] = value
    with pytest.raises(ValueError, match=message):
        validate_residual_round_snapshot(ledger)


def verify_history_origin(origin: str, expected_origin: str) -> None:
    allowed = {expected_origin}
    if expected_origin == "https://github.com/Esbrilltia/ha-csg-plus.git":
        # actions/checkout uses the same GitHub repository URL without .git.
        allowed.add("https://github.com/Esbrilltia/ha-csg-plus")
    require(origin in allowed, "history setup origin differs from approved repository")


def complete_checkout_history(repository: Path, *, expected_origin: str = "https://github.com/Esbrilltia/ha-csg-plus.git") -> None:
    """Explicit test/CLI setup for shallow checkout; validators never fetch.

    Fetch the actual checkout's ancestry without changing refs or HEAD. A missing
    object passed to a validator still fails, including in all negative controls.
    """
    if git_output(repository, "rev-parse", "--is-shallow-repository") == "false":
        return
    head = git_output(repository, "rev-parse", "HEAD")
    verify_history_origin(git_output(repository, "remote", "get-url", "origin"), expected_origin)
    result = subprocess.run(
        ["git", "-C", str(repository), "fetch", "--no-tags", "--no-write-fetch-head",
         "--unshallow", "origin", head],
        capture_output=True, text=True, timeout=90, check=False,
    )
    require(result.returncode == 0, "history setup failed; provide required history")
    require(git_output(repository, "rev-parse", "HEAD") == head
            and git_output(repository, "rev-parse", "--is-shallow-repository") == "false",
            "history setup did not preserve checkout and complete ancestry")


def implementation_snapshot(repository: Path = REPOSITORY_PATH) -> dict:
    snapshot = json.loads(git_output(repository, "show", f"{REVIEWED_HEAD}:docs/quality/historical-findings.json"))
    require(fingerprint(snapshot) == IMPLEMENTATION_SNAPSHOT_HASH,
            "reviewed implementation snapshot drift")
    validate_residual_round_snapshot(snapshot)
    return snapshot


def verify_implementation_evidence(ledger: dict, evidence_root: Path) -> None:
    """Local byte gate; CI preserves these audited bindings without publishing bytes."""
    root = evidence_root.resolve()
    for row in ledger["findings"] + ledger["additional_findings"]:
        if row["implementation"]["status"] != "IMPLEMENTED_PENDING_REVIEW":
            continue
        validate_evidence(row["acceptance"]["evidence"], required=True, label="implementation")
        for evidence in row["acceptance"]["evidence"]:
            path = (root / evidence["ref"]).resolve()
            require(path.is_relative_to(root) and path.is_file(), "implementation evidence file unavailable")
            require(hashlib.sha256(path.read_bytes()).hexdigest() == evidence["sha256"],
                    "implementation evidence byte hash drift")


def verify_production_freeze(repository: Path, reviewed: str = REVIEWED_HEAD) -> None:
    before = git_output(repository, "ls-tree", "-r", "--full-tree", reviewed,
                        "--", "custom_components/csg_plus")
    after = git_output(repository, "ls-tree", "-r", "--full-tree", "HEAD",
                       "--", "custom_components/csg_plus")
    require(bool(before) and before == after, "production path/mode/type/blob drift")


def validate_reviewed_registration(ledger: dict, *, repository: Path = REPOSITORY_PATH) -> dict:
    """Permit exactly the independently approved twelve registrations, preserving all else."""
    records = ledger["findings"] + ledger["additional_findings"]
    require(ledger["ledger_revision"] == REGISTRATION_REVISION, "registration phase revision drift")
    require(len(ledger["additional_findings"]) == 2
            and {r["finding_id"] for r in ledger["additional_findings"]} == APPROVED_ADDITIONS,
            "registration additional scope differs from approved two observations")
    for field, state in (("independent_review", "PASSED"), ("independent_closure", "CLOSED")):
        require({r["finding_id"] for r in records if r[field]["status"] == state} == APPROVED_CLOSURES,
                "registration review/closure set differs from approved twelve")
    require(all(r["risk_acceptance"]["decision"] is None for r in records),
            "registration cannot accept risk")
    for row in records:
        if row["finding_id"] not in APPROVED_CLOSURES:
            continue
        evidence = REGISTRATION_EVIDENCE
        if row["finding_id"] == "IR-B1-03":
            evidence = [REGISTRATION_EVIDENCE[0], *REGISTRATION_EVIDENCE[2:]]
        elif row["finding_id"] == "IR-B1-01":
            evidence = REGISTRATION_EVIDENCE[2:]
        require(row["independent_review"] == {
            "status": "PASSED", "reviewer": REGISTRATION_REVIEWER,
            "reviewed_head": REVIEWED_HEAD, "reviewed_tree": REVIEWED_TREE, "evidence": evidence,
        }, f"{row['finding_id']}: registration review differs from independent advice")
        require(row["independent_closure"] == {
            "status": "CLOSED", "reviewer": REGISTRATION_REVIEWER,
            "closed_commit": REVIEWED_HEAD, "evidence": evidence,
        }, f"{row['finding_id']}: registration closure differs from independent advice")

    before = implementation_snapshot(repository)
    original = {r["finding_id"]: r for r in before["findings"] + before["additional_findings"]}
    normalized = deepcopy(ledger)
    normalized["ledger_revision"] = before["ledger_revision"]
    for row in normalized["findings"] + normalized["additional_findings"]:
        if row["finding_id"] in APPROVED_CLOSURES:
            for field in ("independent_review", "independent_closure"):
                row[field] = deepcopy(original[row["finding_id"]][field])
    require(normalized == before, "registration changed frozen implementation/provenance or unapproved review")
    report = reconcile_ledger(ledger, repository=repository)
    remaining = report["remaining_risk"]
    require(set(remaining["finding_ids"]) == EXPECTED_REMAINING
            and remaining["not_independently_closed_count"] == 15,
            "registration historical remaining set must be exactly fifteen")
    require(remaining["additional_not_closed_count"] == 0,
            "registration additional remaining must be zero")
    verify_production_freeze(repository)
    return report


@pytest.fixture(scope="module", autouse=True)
def registration_history():
    complete_checkout_history(REPOSITORY_PATH)


@pytest.fixture
def ledger() -> dict:
    # Keep all pre-registration controls meaningful on the actual frozen input.
    return implementation_snapshot()


@pytest.fixture
def registered_ledger() -> dict:
    return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def git_objects(tmp_path):
    """A separate real Git object graph; never touch repository refs or tags."""
    repository = tmp_path / "synthetic-binding-repository"
    repository.mkdir()

    def git(*arguments, input=None):
        result = subprocess.run(
            ["git", "-C", str(repository), "-c", "user.name=Synthetic reviewer",
             "-c", "user.email=synthetic@example.invalid", *arguments],
            input=input.encode() if input is not None else None,
            capture_output=True, timeout=15, check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.decode().strip()

    git("init", "--quiet")

    def tree(content):
        blob = git("hash-object", "-w", "--stdin", input=content)
        return git("mktree", input=f"100644 blob {blob}\tsynthetic.txt\n")

    def commit(tree_hash, message, parent=None):
        arguments = ["commit-tree", tree_hash]
        if parent is not None:
            arguments.extend(["-p", parent])
        return git(*arguments, input=message + "\n")

    initial_tree = tree("synthetic base\n")
    candidate_tree = tree("synthetic group implementation\n")
    reviewed_tree = tree("synthetic final implementation\n")
    ledger_tree = tree("synthetic later review record\n")
    base = commit(initial_tree, "test(quality): create synthetic base")
    candidate = commit(candidate_tree, "test(quality): create synthetic group implementation", base)
    reviewed = commit(reviewed_tree, "test(quality): create synthetic reviewed implementation", candidate)
    ledger_commit = commit(ledger_tree, "test(quality): record synthetic review", reviewed)
    sibling = commit(reviewed_tree, "test(quality): create synthetic sibling candidate", base)
    git("update-ref", "HEAD", ledger_commit)
    return SimpleNamespace(repository=repository, git=git, tree=tree, commit=commit,
        base=base, candidate=candidate, reviewed=reviewed, reviewed_tree=reviewed_tree,
        candidate_tree=candidate_tree, ledger_commit=ledger_commit, ledger_tree=ledger_tree,
        sibling=sibling)


def close_synthetic_record(ledger, objects):
    record = next(item for item in ledger["findings"] if item["finding_id"] == "R1-B4")
    evidence = {"ref": "synthetic-independent-review-result", "sha256": "2" * 64}
    record["implementation"] |= {"status": "IMPLEMENTED_PENDING_REVIEW", "candidate_commit": objects.candidate}
    record["acceptance"]["evidence"] = [evidence.copy()]
    record["independent_review"] = {
        "status": "PASSED", "reviewer": "synthetic-reviewer", "reviewed_head": objects.reviewed,
        "reviewed_tree": objects.reviewed_tree, "evidence": [evidence.copy()],
    }
    record["independent_closure"] = {
        "status": "CLOSED", "closed_commit": objects.reviewed,
        "reviewer": "synthetic-reviewer", "evidence": [evidence.copy()],
    }
    return record


def test_historical_set_and_remaining_risk_are_reported_separately(ledger, capsys):
    report = reconcile_ledger(ledger)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    assert report["set_completeness"]["missing"] == []
    assert report["remaining_risk"]["historically_active_count"] == 25
    assert report["remaining_risk"]["validation_boundaries"]["real_beta_A"] == "STOP_INCOMPLETE"
    assert report["remaining_risk"]["validation_boundaries"]["background_integration_disabled"] == "UNVERIFIED"
    assert "set_completeness" in capsys.readouterr().out


@pytest.mark.parametrize("mutation, message", [
    ("omitted", "historical set difference"),
    ("duplicate", "duplicate historical finding"),
    ("replacement_addition", "additional finding replaces historical finding"),
    ("duplicate_addition", "duplicate additional finding"),
    ("source", "source provenance drift"),
    ("severity", "historical metadata drift"),
    ("classification", "historical classification mismatch"),
    ("historical_object", "historical closure must remain bound to beta1"),
    ("historical_commit", "historical metadata drift"),
    ("historical_pr", "historical metadata drift"),
    ("relation", "historical metadata drift"),
    ("owner", "missing owner"),
    ("disposition", "missing disposition"),
    ("criteria", "missing acceptance contract"),
    ("review_conditions", "missing review conditions"),
    ("implementation_closed", "implementation cannot independently close"),
    ("candidate", "missing candidate SHA"),
    ("implementation_evidence", "missing evidence"),
    ("review_without_evidence", "missing evidence"),
    ("unperformed_reviewer", "fabricated unperformed review"),
    ("unperformed_tree", "fabricated unperformed review"),
    ("missing_reviewed_tree", "missing reviewed tree"),
    ("binding_policy", "unknown review binding policy"),
    ("closure_without_review", "closure requires reviewed implementation"),
    ("partial_closure", "incomplete closure fields"),
    ("acceptance", "incomplete acceptance decision"),
])
def test_gate_rejects_loss_or_unsupported_risk_changes(ledger, mutation, message):
    record = next(item for item in ledger["findings"] if item["finding_id"] == "R1-B4")
    if mutation == "omitted":
        ledger["findings"].pop()
    elif mutation == "duplicate":
        ledger["findings"].append(deepcopy(record))
    elif mutation == "replacement_addition":
        ledger["additional_findings"].append(deepcopy(record))
    elif mutation == "duplicate_addition":
        added = deepcopy(record)
        added["finding_id"] = "NEW-1"
        ledger["additional_findings"] = [added, deepcopy(added)]
    elif mutation == "source":
        ledger["sources"][1]["sha256"] = "0" * 64
    elif mutation == "severity":
        record["original_severity"] = "C"
    elif mutation == "classification":
        record["beta1_classification"] = "CLOSED"
    elif mutation == "historical_object":
        record["historical_closure"]["applies_to_commit"] = "0" * 40
    elif mutation == "historical_commit":
        ledger["findings"][0]["historical_closure"]["commits"] = []
    elif mutation == "historical_pr":
        record["historical_closure"]["pull_requests"] = [999]
    elif mutation == "relation":
        record["related_findings"] = ["MISSING-1"]
    elif mutation == "owner":
        record["owner_role"] = ""
    elif mutation == "disposition":
        record["disposition"] = ""
    elif mutation == "criteria":
        record["acceptance"]["criteria"] = []
    elif mutation == "review_conditions":
        record["next_review"]["triggers"] = []
    elif mutation == "implementation_closed":
        record["implementation"]["status"] = "CLOSED"
    elif mutation == "candidate":
        record["implementation"] |= {"status": "IMPLEMENTED_PENDING_REVIEW", "candidate_commit": None}
    elif mutation == "implementation_evidence":
        record["implementation"] |= {"status": "IMPLEMENTED_PENDING_REVIEW", "candidate_commit": "1" * 40}
        record["acceptance"]["evidence"] = []
    elif mutation == "review_without_evidence":
        record["independent_review"] |= {"status": "PASSED", "reviewer": "synthetic-reviewer", "reviewed_head": "1" * 40, "reviewed_tree": "2" * 40, "evidence": []}
    elif mutation == "unperformed_reviewer":
        record["independent_review"] = {"status": "NOT_PERFORMED", "reviewer": "synthetic-reviewer", "reviewed_head": None, "evidence": []}
    elif mutation == "unperformed_tree":
        record["independent_review"]["reviewed_tree"] = "2" * 40
    elif mutation == "missing_reviewed_tree":
        record["independent_review"] |= {"status": "PASSED", "reviewer": "synthetic-reviewer", "reviewed_head": "1" * 40, "evidence": [{"ref": "synthetic-review", "sha256": "2" * 64}]}
        record["independent_review"].pop("reviewed_tree", None)
    elif mutation == "binding_policy":
        ledger["review_binding_policy"] = "candidate_equals_reviewed_head"
    elif mutation == "closure_without_review":
        record["independent_closure"] |= {"status": "CLOSED", "closed_commit": "1" * 40, "reviewer": "synthetic-reviewer", "evidence": [{"ref": "synthetic-review", "sha256": "2" * 64}]}
    elif mutation == "partial_closure":
        record["independent_closure"]["closed_commit"] = "1" * 40
    elif mutation == "acceptance":
        record["risk_acceptance"]["decision"] = "ACCEPTED"
    with pytest.raises(ValueError, match=message):
        reconcile_ledger(ledger)


def test_new_findings_are_reported_separately_without_replacing_history(ledger):
    existing_ids = [row["finding_id"] for row in ledger["additional_findings"]]
    existing_not_closed = sum(row["independent_closure"]["status"] != "CLOSED"
                              for row in ledger["additional_findings"])
    added = deepcopy(ledger["findings"][0])
    added["finding_id"] = "NEW-SYNTHETIC-1"
    added["related_findings"] = ["R1-B4"]
    ledger["additional_findings"].append(added)
    report = reconcile_ledger(ledger)
    assert report["set_completeness"]["located_count"] == 77
    assert report["set_completeness"]["additional_ids"] == sorted(existing_ids + ["NEW-SYNTHETIC-1"])
    assert report["remaining_risk"]["additional_finding_ids"] == sorted(existing_ids + ["NEW-SYNTHETIC-1"])
    assert report["remaining_risk"]["additional_not_closed_count"] == existing_not_closed + 1
    added["related_findings"] = ["UNKNOWN-SYNTHETIC-1"]
    with pytest.raises(ValueError, match="unresolved relation"):
        reconcile_ledger(ledger)


def test_implemented_candidates_still_count_as_pending_independent_review(ledger):
    record = next(item for item in ledger["findings"] if item["finding_id"] == "R1-B4")
    record["implementation"] |= {"status": "IMPLEMENTED_PENDING_REVIEW", "candidate_commit": "1" * 40}
    record["acceptance"]["evidence"] = [{"ref": "synthetic-focused-result", "sha256": "2" * 64}]
    report = reconcile_ledger(ledger)
    assert "R1-B4" in report["remaining_risk"]["finding_ids"]
    assert record["beta1_classification"] == "STILL OPEN"


@pytest.mark.parametrize("missing", ["ref", "sha256"])
def test_independent_closure_requires_complete_matching_evidence(ledger, git_objects, missing):
    record = close_synthetic_record(ledger, git_objects)
    assert "R1-B4" not in reconcile_ledger(ledger, repository=git_objects.repository)["remaining_risk"]["finding_ids"]
    del record["independent_closure"]["evidence"][0][missing]
    with pytest.raises(ValueError, match="missing evidence"):
        reconcile_ledger(ledger, repository=git_objects.repository)


def test_public_ledger_contains_no_private_paths_or_source_payloads(ledger):
    public_text = json.dumps(ledger, ensure_ascii=False)
    assert not re.search(r"[A-Za-z]:[\\/]", public_text)
    assert "local_relative_path" not in public_text
    assert "baseline_record" not in public_text
    assert "original_evidence" not in public_text
    assert "original_description" not in public_text
    assert "authorization_context" not in public_text


def test_closure_accepts_group_ancestor_and_later_review_record_without_tree_self_reference(ledger, git_objects):
    record = close_synthetic_record(ledger, git_objects)
    assert git_objects.candidate != git_objects.reviewed != git_objects.ledger_commit
    assert git_objects.reviewed_tree != git_objects.ledger_tree
    report = reconcile_ledger(ledger, repository=git_objects.repository)
    assert "R1-B4" not in report["remaining_risk"]["finding_ids"]
    assert record["implementation"]["candidate_commit"] == git_objects.candidate
    assert record["independent_review"]["reviewed_head"] == git_objects.reviewed
    assert record["independent_review"]["reviewed_tree"] == git_objects.reviewed_tree


def test_closure_accepts_same_candidate_and_reviewed_commit(ledger, git_objects):
    record = close_synthetic_record(ledger, git_objects)
    record["implementation"]["candidate_commit"] = git_objects.reviewed
    assert "R1-B4" not in reconcile_ledger(ledger, repository=git_objects.repository)["remaining_risk"]["finding_ids"]


@pytest.mark.parametrize("mutation,message", [
    ("same_tree_sibling", "implementation candidate is not an ancestor"),
    ("reversed_ancestry", "implementation candidate is not an ancestor"),
    ("candidate_tree_object", "object is not a commit"),
    ("reviewed_tree_object", "object is not a commit"),
    ("missing_candidate", "Git object unavailable"),
    ("missing_reviewed", "Git object unavailable"),
    ("wrong_reviewed_tree", "reviewed tree does not match"),
    ("checkout_not_descendant", "reviewed HEAD is not an ancestor of ledger checkout"),
    ("closed_at_group", "closure identity mismatch"),
    ("wrong_reviewer", "closure identity mismatch"),
    ("closure_hash_drift", "closure evidence is not bound"),
    ("closure_reference_drift", "closure evidence is not bound"),
])
def test_closure_rejects_actual_git_or_review_binding_drift(ledger, git_objects, mutation, message):
    record = close_synthetic_record(ledger, git_objects)
    if mutation == "same_tree_sibling":
        record["implementation"]["candidate_commit"] = git_objects.sibling
    elif mutation == "reversed_ancestry":
        record["implementation"]["candidate_commit"] = git_objects.ledger_commit
    elif mutation == "candidate_tree_object":
        record["implementation"]["candidate_commit"] = git_objects.candidate_tree
    elif mutation == "reviewed_tree_object":
        record["independent_review"]["reviewed_head"] = git_objects.reviewed_tree
        record["independent_closure"]["closed_commit"] = git_objects.reviewed_tree
    elif mutation == "missing_candidate":
        record["implementation"]["candidate_commit"] = "0" * 40
    elif mutation == "missing_reviewed":
        record["independent_review"]["reviewed_head"] = "0" * 40
        record["independent_closure"]["closed_commit"] = "0" * 40
    elif mutation == "wrong_reviewed_tree":
        record["independent_review"]["reviewed_tree"] = git_objects.candidate_tree
    elif mutation == "checkout_not_descendant":
        git_objects.git("update-ref", "HEAD", git_objects.sibling)
    elif mutation == "closed_at_group":
        record["independent_closure"]["closed_commit"] = git_objects.candidate
    elif mutation == "wrong_reviewer":
        record["independent_closure"]["reviewer"] = "synthetic-other-reviewer"
    elif mutation == "closure_hash_drift":
        record["independent_closure"]["evidence"][0]["sha256"] = "3" * 64
    elif mutation == "closure_reference_drift":
        record["independent_closure"]["evidence"][0]["ref"] = "synthetic-implementation-test"
    with pytest.raises(ValueError, match=message):
        reconcile_ledger(ledger, repository=git_objects.repository)


@pytest.mark.parametrize("reviewed_tree", [None, "", "2" * 39, "g" * 40, False, {}])
def test_performed_review_requires_complete_tree_even_without_closure(ledger, tmp_path, reviewed_tree):
    record = next(item for item in ledger["findings"] if item["finding_id"] == "R1-B4")
    record["independent_review"] |= {
        "status": "PASSED", "reviewer": "synthetic-reviewer", "reviewed_head": "1" * 40,
        "reviewed_tree": reviewed_tree, "evidence": [{"ref": "synthetic-review", "sha256": "2" * 64}],
    }
    with pytest.raises(ValueError, match="missing reviewed tree"):
        reconcile_ledger(ledger, repository=tmp_path / "not-a-repository")


@pytest.mark.parametrize("review_status", ["NOT_PERFORMED", "PASSED", "FAILED"])
def test_unclosed_ledger_does_not_require_historical_git_objects(ledger, tmp_path, review_status):
    record = next(item for item in ledger["findings"] if item["finding_id"] == "R1-B4")
    if review_status != "NOT_PERFORMED":
        record["independent_review"] |= {
            "status": review_status, "reviewer": "synthetic-reviewer", "reviewed_head": "1" * 40,
            "reviewed_tree": "2" * 40, "evidence": [{"ref": "synthetic-review", "sha256": "2" * 64}],
        }
    report = reconcile_ledger(ledger, repository=tmp_path / "not-a-repository")
    assert "R1-B4" in report["remaining_risk"]["finding_ids"]


@pytest.mark.parametrize("field,value", [("ref", []), ("ref", {}), ("ref", True),
    ("sha256", []), ("sha256", {}), ("sha256", False)])
def test_evidence_pair_binding_rejects_unsafe_container_types(ledger, field, value):
    record = next(item for item in ledger["findings"] if item["finding_id"] == "R1-B4")
    record["acceptance"]["evidence"] = [{"ref": "synthetic-evidence", "sha256": "2" * 64}]
    record["acceptance"]["evidence"][0][field] = value
    with pytest.raises(ValueError, match="missing evidence"):
        reconcile_ledger(ledger)


def test_final_registration_preserves_review_authority_and_exact_remaining_sets(registered_ledger):
    report = validate_reviewed_registration(registered_ledger)
    assert report["set_completeness"]["located_count"] == 77
    assert report["remaining_risk"]["historically_active_count"] == 25
    assert set(report["remaining_risk"]["finding_ids"]) == EXPECTED_REMAINING
    assert report["remaining_risk"]["additional_not_closed_count"] == 0
    assert len(APPROVED_HISTORICAL_CLOSURES) == 10
    assert len(APPROVED_ADDITIONS) == 2
    test_public_ledger_contains_no_private_paths_or_source_payloads(registered_ledger)


@pytest.mark.parametrize("mutation", [
    "missing_registration", "extra_registration", "R1-B2", "R2-B4", "M1-B5",
    "missing_addition", "extra_addition", "failed_review", "review_evidence_empty",
    "closure_evidence_empty", "no_evidence_intersection", "wrong_head", "wrong_tree",
    "evidence_ref", "evidence_hash", "reviewer", "closure_commit", "risk",
    "classification", "source", "historical_metadata", "implementation_evidence",
    "implementation_candidate", "severity", "observation", "unapproved_pending_review",
    "remaining_set_swap", "additional_not_closed", "baseline", "risk_owner",
])
def test_registration_rejects_unapproved_fields_and_sets(registered_ledger, mutation):
    rows = {r["finding_id"]: r for r in registered_ledger["findings"] + registered_ledger["additional_findings"]}
    row = rows["R1-B4"]
    if mutation == "missing_registration":
        original = implementation_snapshot()["findings"]
        before = next(r for r in original if r["finding_id"] == "R1-B4")
        for field in ("independent_review", "independent_closure"):
            row[field] = deepcopy(before[field])
    elif mutation in {"extra_registration", "R1-B2", "R2-B4", "M1-B5", "remaining_set_swap"}:
        target = rows[mutation if mutation in {"R1-B2", "R2-B4", "M1-B5"} else "R1-B2"]
        for field in ("independent_review", "independent_closure"):
            target[field] = deepcopy(row[field])
        if mutation == "remaining_set_swap":
            row["independent_closure"]["status"] = "NOT_CLOSED"
    elif mutation == "missing_addition":
        registered_ledger["additional_findings"].pop()
    elif mutation == "extra_addition":
        extra = deepcopy(registered_ledger["additional_findings"][0])
        extra["finding_id"] = "EXTRA-SYNTHETIC"
        registered_ledger["additional_findings"].append(extra)
    elif mutation == "failed_review":
        row["independent_review"]["status"] = "FAILED"
    elif mutation in {"review_evidence_empty", "closure_evidence_empty", "no_evidence_intersection"}:
        field = "independent_review" if mutation == "review_evidence_empty" else "independent_closure"
        row[field]["evidence"] = ([] if mutation != "no_evidence_intersection" else
                                  [{"ref": "synthetic-other-review", "sha256": "3" * 64}])
    elif mutation in {"wrong_head", "wrong_tree", "reviewer"}:
        field = {"wrong_head": "reviewed_head", "wrong_tree": "reviewed_tree", "reviewer": "reviewer"}[mutation]
        row["independent_review"][field] = "0" * 40
    elif mutation in {"evidence_ref", "evidence_hash"}:
        field = "ref" if mutation == "evidence_ref" else "sha256"
        # Drift both arrays together: intersection alone must not establish authority.
        for key in ("independent_review", "independent_closure"):
            row[key]["evidence"][0][field] = "3" * 64
    elif mutation == "closure_commit":
        row["independent_closure"]["closed_commit"] = row["implementation"]["candidate_commit"]
    elif mutation == "risk":
        row["risk_acceptance"]["decision"] = "ACCEPTED"
    elif mutation == "risk_owner":
        row["risk_acceptance"]["owner"] = "synthetic owner without decision"
    elif mutation == "classification":
        row["beta1_classification"] = "CLOSED"
        row["historical_closure"]["classification"] = "CLOSED"
    elif mutation == "source":
        registered_ledger["sources"][0]["sha256"] = "0" * 64
    elif mutation == "historical_metadata":
        row["historical_closure"]["pull_requests"] = [999]
    elif mutation == "implementation_evidence":
        row["acceptance"]["evidence"] = []
    elif mutation == "implementation_candidate":
        row["implementation"]["candidate_commit"] = BASE
    elif mutation == "severity":
        rows["IR-B1-01"]["original_severity"] = "A"
    elif mutation == "observation":
        rows["IR-B1-01"]["source_observation"]["observed_head"] = BASE
    elif mutation == "unapproved_pending_review":
        rows["R1-B2"]["independent_review"]["status"] = "PENDING"
    elif mutation == "additional_not_closed":
        rows["IR-B1-01"]["independent_closure"]["status"] = "NOT_CLOSED"
    elif mutation == "baseline":
        registered_ledger["baseline"]["tree"] = "0" * 40
    with pytest.raises(ValueError, match="registration"):
        validate_reviewed_registration(registered_ledger)


@pytest.mark.parametrize("mutation", ["missing", "hash", "escape"])
def test_local_implementation_evidence_checks_actual_bytes(ledger, tmp_path, mutation):
    evidence = tmp_path / "synthetic-evidence.json"
    evidence.write_bytes(b'{"synthetic":true}\n')
    pair = {"ref": evidence.name, "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}
    for row in ledger["findings"] + ledger["additional_findings"]:
        if row["implementation"]["status"] == "IMPLEMENTED_PENDING_REVIEW":
            row["acceptance"]["evidence"] = [deepcopy(pair)]
    verify_implementation_evidence(ledger, tmp_path)
    if mutation == "missing":
        evidence.unlink()
    elif mutation == "hash":
        evidence.write_bytes(b'{"synthetic":false}\n')
    else:
        for row in ledger["findings"]:
            if row["implementation"]["status"] == "IMPLEMENTED_PENDING_REVIEW":
                row["acceptance"]["evidence"][0]["ref"] = "../outside-evidence.json"
    with pytest.raises(ValueError, match="implementation evidence"):
        verify_implementation_evidence(ledger, tmp_path)


def test_shallow_history_setup_preserves_checkout_and_real_object_gate(git_objects, tmp_path):
    clone = tmp_path / "synthetic-shallow"
    origin = git_objects.repository.as_uri()
    subprocess.run(["git", "clone", "--quiet", "--depth=1", origin, str(clone)],
                   capture_output=True, text=True, timeout=30, check=True)
    head = git_output(clone, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="Git object unavailable"):
        verify_closed_git_binding(clone, git_objects.candidate, git_objects.reviewed, git_objects.reviewed_tree)
    with pytest.raises(ValueError, match="origin differs"):
        complete_checkout_history(clone)
    complete_checkout_history(clone, expected_origin=origin)
    assert git_output(clone, "rev-parse", "HEAD") == head
    verify_closed_git_binding(clone, git_objects.candidate, git_objects.reviewed, git_objects.reviewed_tree)
    with pytest.raises(ValueError, match="Git object unavailable"):
        verify_closed_git_binding(clone, "0" * 40, git_objects.reviewed, git_objects.reviewed_tree)


def test_history_origin_allows_only_the_fixed_repository_and_checkout_url():
    expected = "https://github.com/Esbrilltia/ha-csg-plus.git"
    verify_history_origin(expected, expected)
    verify_history_origin(expected.removesuffix(".git"), expected)
    for other in ("https://github.com/other/ha-csg-plus.git",
                  expected + ".other", "https://example.invalid/Esbrilltia/ha-csg-plus.git"):
        with pytest.raises(ValueError, match="origin differs"):
            verify_history_origin(other, expected)


@pytest.mark.parametrize("mutation", ["content", "path", "mode"])
def test_production_freeze_checks_actual_path_mode_and_blobs(git_objects, mutation):
    def production_tree(path, mode, content):
        blob = git_objects.git("hash-object", "-w", "--stdin", input=content)
        subtree = git_objects.git("mktree", input=f"{mode} blob {blob}\t{path}\n")
        component = git_objects.git("mktree", input=f"040000 tree {subtree}\tcsg_plus\n")
        return git_objects.git("mktree", input=f"040000 tree {component}\tcustom_components\n")

    original_tree = production_tree("synthetic.py", "100644", "synthetic original\n")
    reviewed = git_objects.commit(original_tree, "test(quality): create synthetic production")
    git_objects.git("update-ref", "HEAD", reviewed)
    verify_production_freeze(git_objects.repository, reviewed)
    changed_tree = production_tree("renamed.py" if mutation == "path" else "synthetic.py",
                                   "100755" if mutation == "mode" else "100644",
                                   "synthetic changed\n" if mutation == "content" else "synthetic original\n")
    changed = git_objects.commit(changed_tree, "test(quality): change synthetic production", reviewed)
    git_objects.git("update-ref", "HEAD", changed)
    with pytest.raises(ValueError, match="production path/mode/type/blob drift"):
        verify_production_freeze(git_objects.repository, reviewed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--implementation-evidence-root", type=Path)
    arguments = parser.parse_args()
    complete_checkout_history(REPOSITORY_PATH)
    actual = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    result = validate_reviewed_registration(actual)
    if arguments.implementation_evidence_root is not None:
        verify_implementation_evidence(actual, arguments.implementation_evidence_root)
        result["local_implementation_evidence"] = "ALL_PRIVATE_BYTES_VERIFIED"
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
