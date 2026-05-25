"""0.2.38 — `anthill test` CLI for CI / headless functional testing.

The REPL `/test` requires an interactive prompt ("Run which?
[all/1,3/skip]"). For CI we need a non-interactive flow:

  anthill test "login flow check" --headless --fix 2 \\
      --junit-xml=results.xml --report=report.md

Behavior:
  - Always runs ALL generated cases (no prompt)
  - --headless flips the browser session to headless mode
  - --fix N runs the fix loop on failures (default 0 = no fix)
  - --junit-xml writes a JUnit XML file at the given path
  - --report writes the markdown report at the given path
  - Exits 0 on all-pass, 1 on any failure, 2 on internal error

This is the "anthill in production" entry point. A team wires it
into their pipeline; the human-driven REPL flow stays for local
exploration.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import click

from anthill.config import AnthillConfig
from anthill.core.persistence import load_nation, nation_dir as _nd


@click.command()
@click.argument("source", required=False)
@click.option(
    "--data", "data_file", type=click.Path(),
    help="Data-driven mode: YAML/JSON file with template + rows.",
)
@click.option(
    "--nation", "nation_name", default="default",
    help="Nation to run the test against.",
)
@click.option(
    "--headless/--no-headless", default=True,
    help="Run the browser headlessly (default in CI mode).",
)
@click.option(
    "--fix", "fix_attempts", type=int, default=0,
    help="If >0, auto-fix failures with up to N attempts.",
)
@click.option(
    "--junit-xml", "junit_xml", type=click.Path(),
    help="Write JUnit XML to this path for CI ingestion.",
)
@click.option(
    "--report", "report_path", type=click.Path(),
    help="Write markdown report to this path (otherwise auto-named).",
)
@click.option(
    "--max-cases", type=int, default=20,
    help="Cap the number of generated cases (default 20).",
)
@click.option(
    "--quiet", is_flag=True,
    help="Suppress per-step narration. Final summary only.",
)
def test(
    source: str | None,
    data_file: str | None,
    nation_name: str,
    headless: bool,
    fix_attempts: int,
    junit_xml: str | None,
    report_path: str | None,
    max_cases: int,
    quiet: bool,
) -> None:
    """Run a functional test session from the command line.

    SOURCE can be:
      - inline text:  "test the login flow"
      - file path:    @./prd.md
      - http(s) URL:  https://wiki/PRD-123

    Exit codes:
      0  all tests passed
      1  one or more tests failed
      2  internal error (couldn't generate cases, etc.)
    """
    from anthill.core.qa import (
        FixAttempt,
        TestResult,
        TestSession,
        build_execution_prompt,
        build_fix_prompt,
        expand_data_cases,
        format_junit_xml,
        format_report,
        load_data_table,
        load_requirement,
        parse_cases_response,
        parse_fix_verdict,
        parse_verdict,
        write_junit_xml,
        write_report,
        write_session_json,
    )

    if not source and not data_file:
        click.echo(
            "anthill test: provide SOURCE or --data <file>.",
            err=True,
        )
        sys.exit(2)

    config = AnthillConfig.load()
    config.ensure_home()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        click.echo(
            f"anthill test: nation {nation_name!r} not found. "
            f"Run `anthill init {nation_name}` first.",
            err=True,
        )
        sys.exit(2)

    # Wire flags onto the nation so per-call code paths see them.
    nation._anthill_home = config.home  # type: ignore[attr-defined]
    nation._browser_headless = headless  # type: ignore[attr-defined]

    # 0.2.39 — data-driven mode bypasses the LLM case-generation step.
    if data_file:
        try:
            dt = load_data_table(Path(data_file))
            pre_built_cases = expand_data_cases(dt)
        except (FileNotFoundError, ValueError) as e:
            click.echo(f"anthill test: data load failed: {e}", err=True)
            sys.exit(2)
        if not quiet:
            click.echo(
                f"🧪 anthill test (data-driven) — {data_file} · "
                f"{len(pre_built_cases)} case(s) from {len(dt.rows)} row(s)"
            )
        exit_code = asyncio.run(_run_with_cases(
            nation=nation,
            config=config,
            requirement=f"Data-driven: {data_file}",
            cases=pre_built_cases,
            fix_attempts=fix_attempts,
            junit_xml=junit_xml,
            report_path=report_path,
            quiet=quiet,
        ))
        sys.exit(exit_code)

    # Resolve the requirement.
    if source.startswith(("http://", "https://")):
        try:
            from anthill.core.url_attachments import expand_urls
            block = expand_urls(source)
            if not block.fetched:
                errs = "; ".join(e.reason for e in block.errors) or "no content"
                click.echo(f"anthill test: URL fetch failed: {errs}", err=True)
                sys.exit(2)
            requirement = "\n\n".join(f.text for f in block.fetched)
            source_label = source
        except Exception as e:  # noqa: BLE001
            click.echo(f"anthill test: URL error: {e}", err=True)
            sys.exit(2)
    else:
        requirement, source_label = load_requirement(source, cwd=Path.cwd())
        if not requirement:
            click.echo(
                f"anthill test: couldn't load requirement from {source!r}",
                err=True,
            )
            sys.exit(2)

    if not quiet:
        click.echo(f"🧪 anthill test — {source_label[:60]} ({len(requirement)} chars)")

    # The whole flow is async (nation.run is async).
    exit_code = asyncio.run(_run(
        nation=nation,
        config=config,
        requirement=requirement,
        fix_attempts=fix_attempts,
        junit_xml=junit_xml,
        report_path=report_path,
        max_cases=max_cases,
        quiet=quiet,
    ))
    sys.exit(exit_code)


async def _run(
    *,
    nation,
    config: AnthillConfig,
    requirement: str,
    fix_attempts: int,
    junit_xml: str | None,
    report_path: str | None,
    max_cases: int,
    quiet: bool,
) -> int:
    """LLM-driven path: generate cases from requirement, then run."""
    from anthill.core.qa import (
        CASE_GENERATION_PROMPT,
        parse_cases_response,
    )

    # 0.2.42 — force agentic mode so citizens have tools. Without
    # it the case-execution step has no shell/browser access — the
    # whole `anthill test` flow becomes a no-op narration.
    nation.agentic_mode = True  # type: ignore[attr-defined]

    # Step 1 — generate cases.
    gen_prompt = CASE_GENERATION_PROMPT.replace(
        "{requirement}", requirement.strip()
    )
    try:
        gen_result = await nation.run("qa_plan", gen_prompt)
    except Exception as e:  # noqa: BLE001
        click.echo(f"anthill test: case generation crashed: {e}", err=True)
        return 2

    cases = parse_cases_response(gen_result.output or "")[:max_cases]
    if not cases:
        click.echo("anthill test: couldn't parse test cases from model.", err=True)
        click.echo(f"raw: {(gen_result.output or '')[:400]}", err=True)
        return 2

    if not quiet:
        click.echo(f"  generated {len(cases)} case(s)")

    return await _run_with_cases(
        nation=nation,
        config=config,
        requirement=requirement,
        cases=cases,
        fix_attempts=fix_attempts,
        junit_xml=junit_xml,
        report_path=report_path,
        quiet=quiet,
    )


async def _run_with_cases(
    *,
    nation,
    config: AnthillConfig,
    requirement: str,
    cases: list,
    fix_attempts: int,
    junit_xml: str | None,
    report_path: str | None,
    quiet: bool,
) -> int:
    """Shared executor: given pre-built TestCases, run them all + artifacts."""
    # 0.2.42 — both code paths into _run_with_cases need agentic mode
    # (data-driven entry skips _run entirely, so set here too).
    nation.agentic_mode = True  # type: ignore[attr-defined]
    from anthill.core.qa import (
        FixAttempt,
        TestResult,
        TestSession,
        build_execution_prompt,
        build_fix_prompt,
        format_junit_xml,
        format_report,
        parse_fix_verdict,
        parse_verdict,
        write_junit_xml,
        write_report,
        write_session_json,
    )

    # Step 2 — run all.
    session = TestSession(
        requirement=requirement,
        cases=cases,
        nation_name=nation.name,
    )
    for c in cases:
        if not quiet:
            click.echo(f"  ▶ #{c.id} {c.name}")
        t0 = time.perf_counter()
        actions = [0]
        try:
            exec_prompt = build_execution_prompt(c)
            run_result = await nation.run(
                "qa_execute",
                exec_prompt,
                on_tool_call=lambda _tc: actions.__setitem__(0, actions[0] + 1),
            )
            narrative = run_result.output or ""
            status, reason = parse_verdict(narrative)
            tr = TestResult(
                case=c, status=status, narrative=narrative,
                duration_seconds=time.perf_counter() - t0,
                actions_taken=actions[0],
                error=reason if status != "passed" else None,
            )
        except Exception as e:  # noqa: BLE001
            tr = TestResult(
                case=c, status="errored",
                duration_seconds=time.perf_counter() - t0,
                error=f"{type(e).__name__}: {e}",
            )
        session.results.append(tr)
        if not quiet:
            mark = {
                "passed": "    ✅ PASS",
                "failed": "    ❌ FAIL",
                "errored": "    ⚠️  ERROR",
            }.get(tr.status, "    ?")
            click.echo(f"{mark} ({tr.duration_seconds:.1f}s)")

    # Step 3 — optional fix loop.
    if fix_attempts > 0:
        for tr in session.results:
            if tr.status not in ("failed", "errored"):
                continue
            if not quiet:
                click.echo(f"  🔧 fix-loop #{tr.case.id}")
            for attempt in range(1, fix_attempts + 1):
                fix_t0 = time.perf_counter()
                try:
                    fix_run = await nation.run("qa_fix", build_fix_prompt(tr))
                    fix_status, fix_summary = parse_fix_verdict(fix_run.output or "")
                except Exception as e:  # noqa: BLE001
                    fix_status, fix_summary = "unknown", str(e)
                if fix_status == "unfixable":
                    tr.fix_attempts.append(FixAttempt(
                        attempt=attempt, fix_status="unfixable",
                        fix_summary=fix_summary, rerun_status="skipped",
                        duration_seconds=time.perf_counter() - fix_t0,
                    ))
                    break
                if fix_status == "unknown":
                    tr.fix_attempts.append(FixAttempt(
                        attempt=attempt, fix_status="unknown",
                        fix_summary=fix_summary, rerun_status="skipped",
                        duration_seconds=time.perf_counter() - fix_t0,
                    ))
                    continue
                try:
                    rerun_run = await nation.run(
                        "qa_execute", build_execution_prompt(tr.case),
                    )
                    rerun_narrative = rerun_run.output or ""
                    rerun_status, rerun_reason = parse_verdict(rerun_narrative)
                except Exception as e:  # noqa: BLE001
                    rerun_status, rerun_reason = "errored", str(e)
                    rerun_narrative = ""
                tr.fix_attempts.append(FixAttempt(
                    attempt=attempt, fix_status="fixed",
                    fix_summary=fix_summary,
                    rerun_status=rerun_status,
                    rerun_narrative=rerun_narrative,
                    duration_seconds=time.perf_counter() - fix_t0,
                ))
                if rerun_status == "passed":
                    tr.status = "passed"
                    tr.error = None
                    tr.narrative += (
                        f"\n\n--- after fix (CI attempt {attempt}) ---\n"
                        + rerun_narrative
                    )
                    break
                else:
                    tr.narrative = rerun_narrative
                    tr.error = rerun_reason

    session.ended_at = time.time()
    nd = _nd(config.home, nation.name)

    # Step 4 — write artifacts.
    try:
        if report_path:
            Path(report_path).parent.mkdir(parents=True, exist_ok=True)
            Path(report_path).write_text(format_report(session), encoding="utf-8")
            md_path = Path(report_path)
        else:
            md_path = write_report(session, nd)
        json_path = write_session_json(session, nd)
    except Exception as e:  # noqa: BLE001
        click.echo(f"anthill test: report write failed: {e}", err=True)
        md_path = None
        json_path = None

    if junit_xml:
        try:
            write_junit_xml(session, Path(junit_xml))
            if not quiet:
                click.echo(f"  junit: {junit_xml}")
        except Exception as e:  # noqa: BLE001
            click.echo(f"anthill test: junit write failed: {e}", err=True)

    # Step 5 — final summary + exit code.
    summary = f"{session.passed}/{session.total} passed"
    if session.failed:
        summary += f" · {session.failed} failed"
    errored = sum(1 for r in session.results if r.status == "errored")
    if errored:
        summary += f" · {errored} errored"
    click.echo(f"  ✓ {summary}")
    if md_path:
        click.echo(f"  report: {md_path}")

    # Cleanup browser session if one was started.
    sess = getattr(nation, "_browser_session", None)
    if sess is not None:
        try:
            await sess.close()
        except Exception:  # noqa: BLE001
            pass

    return 0 if session.failed == 0 and errored == 0 else 1
