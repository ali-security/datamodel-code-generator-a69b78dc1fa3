"""Publish or check the selected generation target with model CLI reporting."""

from __future__ import annotations

import sys
import traceback
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from datamodel_code_generator import Error, InvalidClassNameError
from datamodel_code_generator._target_documents import shown

if TYPE_CHECKING:
    from argparse import Namespace
    from collections.abc import Iterable, Iterator, Sequence

    from datamodel_code_generator.__main__ import OutputComparison
    from datamodel_code_generator._api_generation import PlannedTarget
    from datamodel_code_generator._api_types import GeneratedArtifact, GeneratedProject, NextSteps
    from datamodel_code_generator._publication import StagedFile
    from datamodel_code_generator._structured_output import CheckDifferencePayload, TargetPayload
    from datamodel_code_generator._target_config import TargetConfig

_OK: Final = 0
_DIFF: Final = 1
_ERROR: Final = 2


def run_target(  # noqa: PLR0913, PLR0917
    args: Sequence[str],
    namespace: Namespace,
    config: Any,
    pyproject_path: Path | None,
    batch: list[tuple[str, PlannedTarget, str]] | None = None,
    lock: Any = None,
    job: str = "",
) -> int:
    """Generate or check the selected target from the finalized CLI config, reporting errors like model runs.

    A model setting the target needs but the config lacks is refused like a model option conflict, before the run. A
    batch `job` appends the planned target to the targets its `batch` has planned instead of publishing it, and its
    models record into the batch's remote `lock`. A single run publishes its target as a batch of one, so a failed
    publication is reported as for the model runs that publish through the same journal.
    """
    from datamodel_code_generator._api_generation import model_requirement  # noqa: PLC0415

    if requirement := model_requirement(config.openapi_scopes, _selector(config)):
        print(f"Error: {requirement}", file=sys.stderr)  # noqa: T201
        return _ERROR
    try:
        return _run(args, namespace, config, pyproject_path, batch, lock, job)
    except Exception as error:  # noqa: BLE001
        from datamodel_code_generator._target_selection import keyed  # noqa: PLC0415

        _failure(keyed(error, config), encoding=config.encoding)
        return _ERROR


def _selector(config: Any) -> str:
    """Return the option that selects the run's target."""
    return "--generate-server" if config.generate_client is None else "--generate-client"


def _run(  # noqa: PLR0913, PLR0917
    args: Sequence[str],
    namespace: Namespace,
    config: Any,
    pyproject_path: Path | None,
    batch: list[tuple[str, PlannedTarget, str]] | None,
    lock: Any,
    job: str,
) -> int:
    from datamodel_code_generator.__main__ import (  # noqa: PLC0415
        _publish_or_error,  # pyright: ignore[reportPrivateUsage]
        _single_job_plan,  # pyright: ignore[reportPrivateUsage]
        _stage_job_plans,  # pyright: ignore[reportPrivateUsage]
        _target_lockfile,  # pyright: ignore[reportPrivateUsage, reportUnknownVariableType]
        _target_settings,  # pyright: ignore[reportPrivateUsage, reportUnknownVariableType]
        _write_comparison_output,  # pyright: ignore[reportPrivateUsage]
    )
    from datamodel_code_generator._api_generation import plan_target, prepare_target, render_target  # noqa: PLC0415
    from datamodel_code_generator._target_selection import target_of  # noqa: PLC0415

    lockfile = _target_lockfile(config, pyproject_path)
    generator, target = target_of(config, partial(_base, config, namespace, pyproject_path))
    effective = _target_settings(config, args, lockfile)
    if batch is not None:
        effective.resolve_remote_lock(lock)
    if (source := config.url or config.input) is None:
        prepare_target("", effective, generator)
        source = sys.stdin.read()
    if config.check:
        project = render_target(source, model_config=effective, config=target, generator=generator)
        comparison = _compare_target(project, config.output, target.output, config.encoding)
        _write_comparison_output(comparison, namespace.output_format, target=_target_payload(target, project))
        return _DIFF if comparison.differences else _OK
    if config.diff_against is not None:
        effective = effective.model_copy(update={"input_filename": "<input>"})
        baseline = plan_target(config.diff_against, model_config=effective, config=target, generator=generator)
        current = plan_target(
            source, model_config=effective, config=target, generator=generator, timestamp=baseline.timestamp
        )
        comparison = _compare_target(
            current.project, config.output, target.output, config.encoding, baseline=baseline.project
        )
        _write_comparison_output(
            comparison, namespace.output_format, kind="input-diff", target=_target_payload(target, current.project)
        )
        return _DIFF if comparison.differences else _OK
    json_output = namespace.output_format == "json"
    if batch is not None:
        timestamp = next((earlier.timestamp for _, earlier, _ in batch if earlier.timestamp is not None), None)
        planned = plan_target(source, model_config=effective, config=target, generator=generator, timestamp=timestamp)
        if json_output:
            print(_target_json(planned.project, config.output, config.encoding, target))  # noqa: T201
        batch.append((job, planned, _next_step(target, planned.project)))
        return _OK
    planned = plan_target(source, model_config=effective, config=target, generator=generator, publish=True)
    project = planned.project
    output = _target_json(project, config.output, config.encoding, target) if json_output else None
    (staged,) = _stage_job_plans((_single_job_plan(config, pyproject_path),))
    cast("list[Any]", staged.targets).append((job, planned, _next_step(target, project)))
    if (failure := _publish_or_error((staged,))) is not None:
        return int(failure)
    if output is not None:
        print(output)  # noqa: T201
    return _OK


def publish_targets(files: Iterable[StagedFile], targets: Sequence[tuple[str, PlannedTarget, str]]) -> None:
    """Publish the staged files of a batch and the targets its jobs planned through one journal.

    Each target's dependency notice follows the publication on stderr, as after a single run.
    """
    from datamodel_code_generator._api_generation import publish_planned  # noqa: PLC0415

    publish_planned(files, [(job, planned) for job, planned, _ in targets])
    for _, _, notice in targets:
        print(notice, file=sys.stderr)  # noqa: T201


def _shown(path: Path) -> str:
    """Return a path relative to the working directory when it lies inside it."""
    return shown(path, Path.cwd()).as_posix()


def _payload_base(project: GeneratedProject, models: Path) -> Path:
    """Return the directory payload paths are relative to, as in the model payload: the model output or its parent."""
    rendered = [artifact.path for artifact in project.artifacts if artifact.kind == "model"]
    return models.parent if models in rendered or (not rendered and models.suffix) else models


def _target_payload(target: TargetConfig, project: GeneratedProject) -> TargetPayload:
    """Name the generated package and the requirements it needs at run time, as the uv add notice does."""
    from datamodel_code_generator._structured_output import TargetPayload  # noqa: PLC0415

    return TargetPayload(package=target.package, dependencies=list(project.dependencies))


def _target_json(project: GeneratedProject, models: Path, encoding: str, target: TargetConfig) -> str:
    """Emit rendered model and target text, read like their published files, with the existing generation payload.

    As in the model payload, a path is relative to the model output directory; a file outside it has an absolute path.
    The payload's target names the package, its runtime dependencies, and the next steps with paths of the same base.
    """
    from io import BytesIO, TextIOWrapper  # noqa: PLC0415

    from datamodel_code_generator._structured_output import (  # noqa: PLC0415
        EntrypointPayload,
        GeneratedFilePayload,
        GeneratedTargetPayload,
        ImplementationPayload,
        generation_output_json,
    )

    artifacts = [
        (
            artifact.path,
            content,
            encoding if artifact.kind == "model" or artifact.path.suffix in {".py", ".pyi"} else "utf-8",
        )
        for artifact in project.artifacts
        if artifact.kind in {"model", "target"} and (content := artifact.content) is not None
    ]
    base = _payload_base(project, models)
    files = [
        GeneratedFilePayload(
            path=shown(path, base).as_posix(),
            content=TextIOWrapper(BytesIO(content), encoding=codec).read(),
        )
        for path, content, codec in sorted(artifacts, key=lambda artifact: artifact[0].parts)
    ]
    steps = project.next_steps
    entrypoint = implementation = None
    if (app := steps.app) is not None:
        entrypoint = EntrypointPayload(app=app, path=shown(cast("Path", steps.entrypoint), base).as_posix())
        implementation = [
            ImplementationPayload(
                module=item.module, name=item.name, path=shown(item.path, base).as_posix(), created=item.created
            )
            for item in steps.implementations
        ]
    payload = GeneratedTargetPayload(
        package=target.package,
        dependencies=list(project.dependencies),
        readme=shown(steps.readme, base).as_posix(),
        entrypoint=entrypoint,
        implementation=implementation,
        usage=steps.usage,
    )
    return generation_output_json(files, output=models.as_posix(), target=payload)


def _nested(models: Path, target: Path) -> dict[str, tuple[Path, Path]]:
    """Return the output root that lies inside the other one, by artifact kind, with its path inside that root.

    A comparison takes that root as part of the other one, so each file is reported once and neither root lists
    the files of the other as extra.
    """
    model_root, target_root = models.resolve(), target.resolve()
    if model_root.is_relative_to(target_root):
        return {"model": (models, model_root.relative_to(target_root))}
    if target_root.is_relative_to(model_root):
        return {"target": (target, target_root.relative_to(model_root))}
    return {}


def _compare_target(
    project: GeneratedProject,
    models: Path,
    target: Path,
    encoding: str,
    *,
    baseline: GeneratedProject | None = None,
) -> OutputComparison:
    """Compare rendered text and Python output roots through the model comparison path.

    Labels stay relative to the working directory; each difference names its file like the generation payload. With
    a `baseline` rendered from another input, the comparison is an input diff against its files instead of the output.
    """
    from tempfile import TemporaryDirectory  # noqa: PLC0415

    from datamodel_code_generator.__main__ import (  # noqa: PLC0415
        OutputComparison,
        OutputComparisonOptions,
        _compare_generated_outputs,  # pyright: ignore[reportPrivateUsage]
    )

    nested = _nested(models, target)
    differences: list[CheckDifferencePayload] = []
    contents: list[str] = []
    base, cwd = _payload_base(project, models), Path.cwd()
    with TemporaryDirectory(prefix="datamodel-codegen-check-") as directory:
        staging = Path(directory)
        baseline_root = None if baseline is None else staging / "baseline"
        for artifact in () if baseline is None else _generated(baseline):
            (staged := _existing(artifact.path, baseline_root)).parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(artifact.content)
        rendered = [*project.artifacts, *(() if baseline is None else baseline.artifacts)]
        for kind, output, is_directory in (("model", models, base == models), ("target", target, True)):
            if not is_directory and not any(artifact.kind == kind for artifact in rendered):
                continue
            if kind in nested:
                continue
            staged_root = staging / kind
            staged_root.mkdir()
            non_python: list[tuple[Path, Path, str]] = []
            for artifact in project.artifacts:
                path = artifact.path
                if artifact.kind == kind:
                    staged = staged_root / (path.relative_to(output) if is_directory else path.name)
                elif (inner := nested.get(artifact.kind)) is not None:
                    staged = staged_root / inner[1] / path.relative_to(inner[0])
                else:
                    continue
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_bytes(artifact.content)
                if is_directory and path.suffix != ".py":
                    non_python.append((staged, path, encoding if artifact.kind == "model" else "utf-8"))
            _stage_implementations(project, output if is_directory and baseline is None else None, staged_root)
            comparisons = [(staged_root if is_directory else staged_root / output.name, output, is_directory, encoding)]
            comparisons.extend((staged, path, False, codec) for staged, path, codec in non_python)
            for generated, actual, directory_output, codec in comparisons:
                try:
                    compared = _compare_generated_outputs(
                        generated,
                        _existing(actual, baseline_root),
                        codec,
                        OutputComparisonOptions(
                            is_directory_output=directory_output,
                            input_diff=baseline is not None,
                            single_file_display_path=_shown(actual),
                            directory_display_path=_shown(actual),
                        ),
                    )
                except UnicodeError as error:
                    message = f"{_shown(actual)}: Output is not text in encoding {codec!r}: {error}"
                    raise Error(message) from error
                differences.extend(
                    difference.model_copy(update={"path": shown(cwd / difference.path, base).as_posix()})
                    for difference in compared.differences
                )
                if content := compared.content:
                    contents.append(content if content.endswith("\n") else content + "\n")
    return OutputComparison(differences=differences, content="".join(contents))


def _generated(project: GeneratedProject) -> Iterator[GeneratedArtifact]:
    """Yield the artifacts of a project that generation owns, leaving out the user modules it writes only once."""
    return (artifact for artifact in project.artifacts if artifact.kind != "implementation")


def _stage_implementations(project: GeneratedProject, output: Path | None, staged_root: Path) -> None:
    """Stage the user modules inside a compared output directory as they are, so it never lists them as extra.

    A user module beside a server package lies in the models directory that holds the package. Without an output
    directory to compare with the files at their paths, nothing is staged.
    """
    if output is None:
        return
    root = output.resolve()
    for artifact in project.artifacts:
        if artifact.kind != "implementation":
            continue
        if (location := artifact.path.resolve()).is_relative_to(root) and location.is_file():
            (staged := staged_root / location.relative_to(root)).parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(location.read_bytes())


def _existing(path: Path, baseline_root: Path | None) -> Path:
    """Return the file an output path is compared with: itself, or the baseline file staged for it.

    A baseline root mirrors the filesystem, so every output path, inside the working directory or not, has its place.
    """
    return path if baseline_root is None else baseline_root.joinpath(*Path.cwd().joinpath(path).parts[1:])


def _next_step(target: TargetConfig, project: GeneratedProject) -> str:
    """Return the uv command that adds the generated package's runtime dependencies, then what to do next.

    A server names the modules to implement, marking those the run creates, and how to run its entrypoint; a client
    shows a call of its first operation. Both name their README.
    """
    arguments = " ".join(f'"{dependency}"' for dependency in project.dependencies)
    lines = [f"Add the runtime dependencies of {target.package} to your project:", f"  uv add {arguments}"]
    return "\n".join((*lines, *_steps(project.next_steps)))


def _steps(steps: NextSteps) -> list[str]:
    readme = _shown(steps.readme)
    if steps.usage is not None:
        return [
            "Next steps: call the API with the client:",
            *(f"  {line}".rstrip() for line in steps.usage.splitlines()),
            f"See {readme}.",
        ]
    files = ", ".join(f"{_shown(item.path)}{' (created)' if item.created else ''}" for item in steps.implementations)
    return [
        f"Next steps: {f'implement {files}, then ' if files else ''}run the server:",
        f"  uv add uvicorn && uv run uvicorn {steps.app} --reload",
        f'or `fastapi dev {_shown(cast("Path", steps.entrypoint))}` with "fastapi[standard]"; see {readme}.',
    ]


def _base(config: Any, namespace: Namespace, pyproject_path: Path | None, field: str) -> Path:
    """Return the directory that the documents a setting names resolve against.

    A setting read from a JSON file resolves against that file's directory, and against a link's directory when
    the file is given through one. Inline JSON and tables resolve against the pyproject.toml directory for its keys,
    and against the working directory for options.
    """
    if (source := config._json_sources.get(field)) is not None:  # noqa: SLF001
        from datamodel_code_generator.json_config import _json_file  # noqa: PLC0415  # pyright: ignore[reportPrivateUsage]

        if (file := _json_file(source)) is not None:
            return (Path.cwd() / file).parent
    return Path.cwd() if pyproject_path is None or getattr(namespace, field) is not None else pyproject_path.parent


def _failure(error: Exception, *, encoding: str = "utf-8") -> None:
    """Preserve model CLI error hints and unexpected-error tracebacks."""
    message = str(error)
    if isinstance(error, InvalidClassNameError):
        message = f"{error} You have to set `--class-name` option"
    elif isinstance(error, UnicodeDecodeError):
        message = f"Unable to decode input using encoding {encoding!r}: {error}"
    elif not isinstance(error, (Error, OSError)):
        from datamodel_code_generator.remote_lock import RemoteLockError  # noqa: PLC0415

        if not isinstance(error, RemoteLockError):
            print("".join(traceback.format_exception(error)), file=sys.stderr)  # noqa: T201
            return
    print(f"Error: {message}", file=sys.stderr)  # noqa: T201
