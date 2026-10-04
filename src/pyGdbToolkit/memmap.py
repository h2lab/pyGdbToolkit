"""Bounded, read-only memory sampling with explicit evidence and uncertainty."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Callable

from .arch.memmap import ExecutionHint, MemoryBaseline, MemoryRegion, TargetFingerprint
from .target_memory import TargetMemory, TargetReadError


@dataclass(frozen=True)
class MemoryObservation:
    """One actual target read, without extrapolation to neighboring addresses."""

    address: int
    size: int
    status: str
    error: str | None = None


@dataclass
class MemoryMapReport:
    """Metadata and samples scoped to one debug access context."""

    architecture: str
    context: dict[str, Any]
    regions: list[MemoryRegion] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    dap: dict[str, Any] | None = None
    observations: list[MemoryObservation] = field(default_factory=list)
    probe_runs: list[dict[str, Any]] = field(default_factory=list)
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    fingerprint: TargetFingerprint | None = None
    candidates: list[MemoryBaseline] = field(default_factory=list)
    execution_hints: list[ExecutionHint] = field(default_factory=list)

    def execution_assessments(self) -> list[dict[str, Any]]:
        """Associate runtime addresses with known regions and candidate bases separately."""
        result = []
        for hint in self.execution_hints:
            address = (
                hint.address - 1
                if hint.role in ("stack", "stack-initial") and hint.address
                else hint.address
            )
            matches = [
                region.to_dict() for region in self.regions if region.start <= address < region.end
            ]
            candidates = [
                candidate.to_dict()
                for candidate in self.candidates
                if candidate.start <= address < candidate.end
            ]
            result.append(
                dict(
                    asdict(hint),
                    regions=matches,
                    candidates=candidates,
                    status=(
                        "region-associated"
                        if matches
                        else "candidate-only" if candidates else "unmapped-address"
                    ),
                )
            )
        return result

    def candidate_assessments(self) -> list[dict[str, Any]]:
        """Challenge hypotheses with declarations and exact samples, not extrapolation."""
        assessments = []
        for candidate in self.candidates:
            declarations = [
                region.to_dict()
                for region in self.regions
                if region.source != "elf"
                and candidate.start < region.end
                and region.start < candidate.end
            ]
            samples = [
                sample
                for sample in self.observations
                if candidate.start <= sample.address < candidate.end
            ]
            conflicts = any(
                candidate.kind != "unknown"
                and declaration["kind"]
                not in (
                    ("rom", "flash", "unknown")
                    if candidate.kind == "rom-or-flash"
                    else (candidate.kind, "unknown")
                )
                for declaration in declarations
            )
            hints = [
                hint.name
                for hint in self.execution_hints
                if hint.role in ("code", "handler", "vector-table")
                and candidate.start <= hint.address < candidate.end
            ]
            assessments.append(
                dict(
                    candidate.to_dict(),
                    status=(
                        "kind-conflict"
                        if conflicts
                        else (
                            "declared-overlap"
                            if declarations
                            else "runtime-supported" if hints else "unconfirmed"
                        )
                    ),
                    declarations=declarations,
                    execution_origins=hints,
                    readable_points=sum(sample.status == "readable-sampled" for sample in samples),
                    read_errors=sum(sample.status == "read-error" for sample in samples),
                )
            )
        return assessments

    def to_dict(self) -> dict[str, Any]:
        """Export metadata and measured evidence without asserting full coverage."""
        return {
            "schema_version": 1,
            "generated_at": self.generated_at,
            "architecture": self.architecture,
            "context": self.context,
            "regions": [region.to_dict() for region in self.regions],
            "notes": self.notes,
            "dap": self.dap,
            "observations": [asdict(observation) for observation in self.observations],
            "probe_runs": self.probe_runs,
            "fingerprint": None if self.fingerprint is None else self.fingerprint.to_dict(),
            "candidates": self.candidate_assessments(),
            "execution_hints": self.execution_assessments(),
        }


def probe_regions(
    report: MemoryMapReport,
    memory: TargetMemory,
    regions: list[MemoryRegion],
    *,
    stride: int = 4096,
    max_reads: int = 256,
    timeout: float = 5.0,
    clock: Callable[[], float] = monotonic,
) -> None:
    """Sample exact points under global limits; never write or infer protection.

    The time budget is checked between reads. A blocked backend read must be
    interrupted by the debugger's transport timeout or by the user.
    """
    if stride < 4 or stride % 4:
        raise ValueError("stride must be a positive multiple of 4")
    if not 1 <= max_reads <= 4096:
        raise ValueError("max-reads must be within 1..4096")
    if not 0 < timeout <= 300:
        raise ValueError("timeout must be within (0, 300] seconds")
    if any(region.start % 4 for region in regions):
        raise ValueError("probe range starts must be 4-byte aligned")
    started = clock()
    seen: set[int] = set()
    count = 0
    stop = "complete"
    for region in regions:
        for address in range(region.start, region.end, stride):
            if address in seen:
                continue
            if count >= max_reads or clock() - started >= timeout:
                stop = "max-reads" if count >= max_reads else "timeout"
                break
            size = min(4, region.end - address)
            try:
                memory.read_bytes(address, size)
            except TargetReadError as error:
                observation = MemoryObservation(address, size, "read-error", str(error))
            else:
                observation = MemoryObservation(address, size, "readable-sampled")
            report.observations.append(observation)
            seen.add(address)
            count += 1
        if stop != "complete":
            break
    report.probe_runs.append(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ranges": [{"start": region.start, "end": region.end} for region in regions],
            "stride": stride,
            "max_reads": max_reads,
            "timeout": timeout,
            "reads": count,
            "stop_reason": stop,
        }
    )
