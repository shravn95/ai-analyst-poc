from pathlib import Path

import typer

from analyst.agent.planner import make_plan
from analyst.agent.sqlgen import answer_with_sql
from analyst.agent.synthesizer import synthesize_answer
from analyst.agent.router import route_question
from analyst.agent.context import schema_brief
from analyst.ingest.loaders import load_file
from analyst.llm import PipelineError, call_llm
from analyst.profile.datacard import build_card, ensure_card, render_card_text, save_card
from analyst.profile.typefix import fix_types
from analyst.store.duckdb_store import (
    connect, get_current_session, list_tables, new_session,
    set_current_session, write_tables,
)

app = typer.Typer(help="AI Data Analyst (CLI)")


def _print_tables(infos: list[dict]) -> None:
    for t in infos:
        typer.echo(
            f"  {t['name']:<28} {t['n_rows']:>10,} rows x {t['n_cols']:<3} cols   ({t['source']})"
        )


def _fail(e: Exception):
    typer.secho(f"Error: {e}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


@app.command()
def ping():
    """Check that the LLM connection works."""
    reply = call_llm("router", [{"role": "user", "content": "Reply with the single word: ready"}])
    typer.echo(f"Model replied: {reply}")


@app.command()
def ingest(
    path: Path = typer.Argument(..., help="CSV / Excel / JSON / TXT file"),
    append: bool = typer.Option(False, "--append", help="Add to the current session instead of starting a new one"),
):
    """Load a data file into a DuckDB session."""
    try:
        tables = load_file(path)
        session_id = get_current_session() if append else new_session()
    except (ValueError, RuntimeError) as e:
        _fail(e)

    con = connect(session_id)
    try:
        new_names = write_tables(con, tables)
        changes = fix_types(con, new_names)
        infos = [t for t in list_tables(con) if t["name"] in set(new_names)]
        card = build_card(con, session_id)
        save_card(session_id, card)
    finally:
        con.close()

    set_current_session(session_id)
    typer.echo(f"Session {session_id} {'updated' if append else 'created'}")
    _print_tables(infos)
    if changes:
        typer.echo("Type fixes:")
        for c in changes:
            lost = f"  ({c['values_lost']} unparseable values set to NULL)" if c["values_lost"] else ""
            typer.echo(f"  {c['table']}.{c['column']} -> {c['to']}{lost}")


@app.command()
def tables():
    """List tables in the current session."""
    try:
        session_id = get_current_session()
    except RuntimeError as e:
        _fail(e)

    con = connect(session_id)
    try:
        infos = list_tables(con)
    finally:
        con.close()
    typer.echo(f"Session {session_id}")
    _print_tables(infos)


@app.command()
def summary(refresh: bool = typer.Option(False, "--refresh", help="Rebuild the data card")):
    """Print the data card for the current session."""
    try:
        session_id = get_current_session()
    except RuntimeError as e:
        _fail(e)

    card = ensure_card(session_id, refresh=refresh)
    typer.echo(f"Session {session_id}\n")
    typer.echo(render_card_text(card))


@app.command(name="plan")
def plan_cmd(question: str = typer.Argument(..., help="Question about the loaded data")):
    """Route a question and show the JSON plan (step 4 test command)."""
    try:
        session_id = get_current_session()
    except RuntimeError as e:
        _fail(e)

    card = ensure_card(session_id)
    try:
        route = route_question(question, card)
        typer.echo(f"Route: {route.intent}  ({route.reason})")
        if route.intent == "clarify":
            typer.echo(f"Clarifying question: {route.clarifying_question}")
        if route.intent != "data_query":
            return
        plan = make_plan(question, card)
    except PipelineError as e:
        _fail(e)

    typer.echo(plan.model_dump_json(indent=2))


@app.command()
def ask(question: str = typer.Argument(..., help="Question about the loaded data")):
    """Route -> plan -> SQL -> synthesised answer (step 5/6 test command)."""
    try:
        session_id = get_current_session()
    except RuntimeError as e:
        _fail(e)

    card = ensure_card(session_id)
    try:
        route = route_question(question, card)
        if route.intent == "clarify":
            typer.echo(route.clarifying_question)
            return
        if route.intent != "data_query":
            typer.echo(f"[{route.intent}] {route.reason}")
            return
        plan = make_plan(question, card)
        if plan.needs_clarification:
            typer.echo(plan.clarifying_question)
            return
        outcome = answer_with_sql(question, plan, card, session_id)
    except PipelineError as e:
        _fail(e)

    if plan.assumptions:
        typer.echo("Assumptions: " + "; ".join(plan.assumptions))
    typer.echo(f"\nSQL (attempt {outcome.attempts}):\n{outcome.result.sql}\n")
    for err in outcome.errors:
        typer.secho(f"  retried after: {err[:150]}", fg=typer.colors.YELLOW)

    df = outcome.result.df
    if df.empty:
        typer.echo("(no rows returned)")
    else:
        typer.echo(df.head(25).to_string(index=False))
        if len(df) > 25 or outcome.result.truncated:
            more = "1000+" if outcome.result.truncated else f"{len(df):,}"
            typer.echo(f"... showing 25 of {more} rows")

    # ---- synthesise a natural-language answer ----
    typer.echo("")
    try:
        answer = synthesize_answer(question, outcome, card, plan.assumptions)
        typer.secho(answer, fg=typer.colors.GREEN)
    except PipelineError as e:
        typer.secho(f"(synthesis failed: {e})", fg=typer.colors.YELLOW)


# ---------------------------------------------------------------------------
# Step 6: interactive chat loop
# ---------------------------------------------------------------------------

def _handle_schema_question(question: str, card: dict, history: list[dict]) -> str:
    """Answer schema / metadata questions directly from the data card."""
    system = (
        "You are a helpful data assistant. The user asked about the dataset schema. "
        "Answer using ONLY the schema information below. Be concise.\n\n"
        f"DATA SCHEMA\n{schema_brief(card)}"
    )
    return (call_llm("synthesizer", [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]) or "").strip()


def _handle_chitchat(question: str) -> str:
    """Handle greetings, thanks, and small talk."""
    return (call_llm("synthesizer", [
        {"role": "system",
         "content": "You are a friendly data-analysis assistant. Respond to the "
                    "user's greeting or small talk briefly and warmly. Remind them "
                    "they can ask questions about their loaded data."},
        {"role": "user", "content": question},
    ]) or "").strip()


@app.command()
def chat():
    """Interactive chat — ask questions about the dataset (step 6)."""
    try:
        session_id = get_current_session()
    except RuntimeError as e:
        _fail(e)

    card = ensure_card(session_id)

    typer.secho("╭─ AI Data Analyst ─────────────────────────────────╮", fg=typer.colors.CYAN)
    typer.secho("│  Type your questions. Commands:                   │", fg=typer.colors.CYAN)
    typer.secho("│    /tables   — list tables                        │", fg=typer.colors.CYAN)
    typer.secho("│    /schema   — show data card                     │", fg=typer.colors.CYAN)
    typer.secho("│    /sql      — show last SQL                      │", fg=typer.colors.CYAN)
    typer.secho("│    /clear    — clear conversation history          │", fg=typer.colors.CYAN)
    typer.secho("│    /quit     — exit                               │", fg=typer.colors.CYAN)
    typer.secho("╰───────────────────────────────────────────────────╯", fg=typer.colors.CYAN)
    typer.echo("")

    history: list[dict] = []          # [{role, content}, …]
    last_sql: str | None = None       # for /sql command

    while True:
        try:
            question = typer.prompt("You", prompt_suffix=" > ").strip()
        except (KeyboardInterrupt, EOFError):
            typer.echo("\nBye!")
            break

        if not question:
            continue

        # ---- slash commands ----
        low = question.lower()
        if low in ("/quit", "/exit", "/q"):
            typer.echo("Bye!")
            break
        if low == "/tables":
            con = connect(session_id)
            try:
                _print_tables(list_tables(con))
            finally:
                con.close()
            continue
        if low == "/schema":
            typer.echo(render_card_text(card))
            continue
        if low == "/sql":
            if last_sql:
                typer.echo(last_sql)
            else:
                typer.echo("(no SQL executed yet)")
            continue
        if low == "/clear":
            history.clear()
            last_sql = None
            typer.secho("History cleared.", fg=typer.colors.CYAN)
            continue

        # ---- route the question ----
        try:
            route = route_question(question, card, history=history)
        except PipelineError as e:
            typer.secho(f"Error: {e}", fg=typer.colors.RED)
            continue

        answer: str = ""

        if route.intent == "chitchat":
            answer = _handle_chitchat(question)

        elif route.intent == "schema_question":
            answer = _handle_schema_question(question, card, history)

        elif route.intent == "out_of_scope":
            answer = f"I can't answer that from the loaded data. {route.reason}"

        elif route.intent == "clarify":
            answer = route.clarifying_question or "Could you be more specific?"

        elif route.intent == "data_query":
            try:
                plan = make_plan(question, card, history=history)
                if plan.needs_clarification:
                    answer = plan.clarifying_question or "Could you clarify?"
                else:
                    outcome = answer_with_sql(
                        question, plan, card, session_id, history=history
                    )
                    last_sql = outcome.result.sql

                    # Show concise result table
                    df = outcome.result.df
                    if not df.empty:
                        preview = df.head(15).to_string(index=False)
                        typer.echo(f"\n{preview}")
                        if len(df) > 15 or outcome.result.truncated:
                            more = "1,000+" if outcome.result.truncated else f"{len(df):,}"
                            typer.echo(f"... showing 15 of {more} rows")

                    # Synthesise the natural language answer
                    answer = synthesize_answer(
                        question, outcome, card, plan.assumptions, history=history
                    )

                    if outcome.errors:
                        for err in outcome.errors:
                            typer.secho(f"  (retried: {err[:120]})", fg=typer.colors.YELLOW)
            except PipelineError as e:
                answer = f"Sorry, I couldn't answer that: {e}"

        # ---- print the answer and update history ----
        typer.echo("")
        typer.secho(f"Analyst > {answer}", fg=typer.colors.GREEN)
        typer.echo("")

        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": answer})

        # Keep history bounded (last 20 messages = 10 turns)
        if len(history) > 20:
            history = history[-20:]


if __name__ == "__main__":
    app()