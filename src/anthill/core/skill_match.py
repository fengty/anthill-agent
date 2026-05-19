"""0.1.42 — search saved skills BEFORE planning, distill after success.

The user's correction to the citizens-serve-the-king arc:

  > "缺少关键的动作 — 禅道有可用的 skill 时，没有先去找；其次完成
  > 了事情，要沉淀 skill."

Translation in mechanism terms:

1. **Look first**: when a request comes in, search the nation's saved
   recipes (Anthill's existing analog of "skills") for a match. If
   one fits, use its pre-built plan instead of letting Scout
   regenerate the same shape each time.

2. **Distill after**: when a complex task completes successfully
   (especially after the 0.1.40 refusal-retry path), propose to
   save the approach as a new skill named for what the user just
   asked. The next time a similar URL / question shows up, the
   skill fires immediately.

Mirrors:
  - Claude Code's user-authored `.claude/skills/*.md` + auto-memory
  - Hermes's "agent creates skills from successful complex tasks"
  - CrewAI's role-as-skill, but at a finer grain (per recipe)

This module is pure-stdlib similarity matching. The recipe / scout
integration lives in nation.ask; the post-success distillation hint
lives in the REPL post-ask hook. Both wired in this patch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from anthill.core.recipes import Recipe, list_recipes


# Tokens we match against. Matches core/episodic._tokenize so skill
# matching and episodic search agree on what "similar" means.
_TOKEN_RE = re.compile(r"[一-鿿]|[a-zA-Z0-9_]+")

# Threshold to consider a saved skill applicable. Above this we
# surface a "📚 using skill X" line. Empirically chosen — high
# enough that "translate this to French" doesn't match "translate
# the meeting notes," low enough that "analyze zentao bug 12345"
# matches "analyze zentao bug 67890."
MATCH_CONFIDENCE_THRESHOLD = 0.55

# Min text length for skill matching to even try. Below this the
# request is so short that any match is coincidence.
MIN_REQUEST_TOKENS = 3


@dataclass(frozen=True)
class SkillMatch:
    """One saved skill that fits the current request."""

    recipe: Recipe
    confidence: float           # [0, 1] set-cosine similarity
    matched_via: str            # "name" / "description" / "template"


def _tokens(text: str) -> set[str]:
    """Lowercase token bag. Same shape as core/episodic._tokenize so
    skill matching agrees with episodic recall."""
    if not text:
        return set()
    return {tok.lower() for tok in _TOKEN_RE.findall(text)}


def _cosine(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    common = len(a & b)
    return common / ((len(a) * len(b)) ** 0.5)


def find_matching_skill(
    request: str,
    nation_dir: Path,
    *,
    threshold: float = MATCH_CONFIDENCE_THRESHOLD,
) -> SkillMatch | None:
    """Pick the best-fit saved recipe for ``request``, if any.

    Searches over: recipe.name, recipe.description, recipe.template.
    The MAX of those three similarities is the recipe's score; the
    best-scoring recipe above threshold wins.

    Returns None when the request is too short, no recipes exist,
    or no recipe scores above threshold. Tolerates fs errors; never
    raises (a skill miss must not block planning).
    """
    req_tokens = _tokens(request)
    if len(req_tokens) < MIN_REQUEST_TOKENS:
        return None
    # 0.1.69 — also compute tokens of the SEEDED request, so a recipe
    # whose template contains literal placeholders ("{url}") can match
    # an incoming request that has the corresponding substituted value
    # ("http://..."). Without this, the cosine-on-raw-tokens approach
    # gave low scores for the exact case skills are designed to handle:
    # template "analyze {url}" vs request "analyze http://example.com/...".
    # Symmetrically the recipe template is already in seed form, so
    # matching seed-against-seed bridges the gap.
    seeded_req_tokens = _tokens(_template_seed(request))
    try:
        recipes = list_recipes(nation_dir)
    except Exception:  # noqa: BLE001
        return None
    if not recipes:
        return None

    best: SkillMatch | None = None
    for recipe in recipes:
        name_tokens = _tokens(recipe.name)
        desc_tokens = _tokens(recipe.description)
        template_tokens = _tokens(recipe.template)
        scores = {
            "name": max(
                _cosine(name_tokens, req_tokens),
                _cosine(name_tokens, seeded_req_tokens),
            ),
            "description": max(
                _cosine(desc_tokens, req_tokens),
                _cosine(desc_tokens, seeded_req_tokens),
            ),
            "template": max(
                _cosine(template_tokens, req_tokens),
                _cosine(template_tokens, seeded_req_tokens),
            ),
        }
        best_kind = max(scores, key=lambda k: scores[k])
        best_score = scores[best_kind]
        if best_score < threshold:
            continue
        if best is None or best_score > best.confidence:
            best = SkillMatch(
                recipe=recipe,
                confidence=best_score,
                matched_via=best_kind,
            )
    return best


# ---------------------------------------------------------------------------
# Post-success distillation: when nothing matched, suggest saving the approach
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DistillationSuggestion:
    """The "save this as a skill?" prompt material."""

    suggested_name: str         # snake_case slug from the request
    template_seed: str          # the original request, with the most-variable
                                # parts replaced by {placeholders}
    description: str            # one-line summary in human terms
    subtask_signature: list[str]  # task_types from the plan, for traceability


# Tokens we treat as "variable" — IDs, URLs, dates. Replaced by
# placeholders when generating a template seed so the skill matches
# future related asks instead of being scoped to ONE exact instance.
#
# Order matters: more-specific patterns first. The {date} pattern
# overlaps with {id} (year prefix is 4 digits) so dates must win.
_VARIABLE_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"https?://\S+"), "{url}"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "{date}"),
    (re.compile(r"\b\d{4,}\b"), "{id}"),
)


def _suggested_name(request: str, max_words: int = 5) -> str:
    """snake_case slug from the head of the request. Used for the
    auto-generated skill name; user can rename on save."""
    words = re.findall(r"[a-zA-Z]+|[一-鿿]+", request)
    cleaned: list[str] = []
    for w in words:
        if w.lower() in ("a", "an", "the", "and", "or", "of", "in", "on", "to", "for", "this", "that"):
            continue
        cleaned.append(w.lower() if w.isascii() else w)
        if len(cleaned) >= max_words:
            break
    if not cleaned:
        return "untitled-skill"
    return "-".join(cleaned)


def _template_seed(request: str) -> str:
    """Replace URLs / IDs / dates with placeholders so the recipe
    template matches future related requests, not just this one."""
    seed = request
    for pattern, placeholder in _VARIABLE_PATTERNS:
        seed = pattern.sub(placeholder, seed)
    return seed.strip()


# 0.1.69 — inverse of _template_seed: extract the variable VALUES
# from a fresh request so they can be substituted into a saved
# recipe's `{url}` / `{id}` / `{date}` placeholders.
#
# Bug it fixes: 0.1.42 auto-distillation saved templates with
# placeholders, but `nation.ask`'s skill-match path handed the raw
# prompt_template (literal `{url}`) to citizens. The citizens
# correctly said "{url} isn't a reachable address" — skills were
# theatrical, never actually parameterized.
#
# Placeholder → match function. Each takes the request and returns
# the first match's value, or None. Order matches _VARIABLE_PATTERNS.
def extract_variables(request: str) -> dict[str, str]:
    """Pull values for {url} / {id} / {date} from a request.

    Returns the dict in the shape Recipe.fill() wants — keys are
    placeholder names (without braces), values are the matched
    strings. Missing variables just don't appear in the dict;
    Recipe.fill raises KeyError on a literal placeholder still
    in the template, so callers should populate every placeholder
    a recipe declares.

    Note: the order of substitution mirrors `_template_seed` —
    URL first, then date, then numeric id. That's the same
    precedence that produced the template; reversing it would
    mean (e.g.) `2026-05-19` getting matched as `id=2026` and
    leaving `-05-19` as orphan text.
    """
    out: dict[str, str] = {}
    for pattern, placeholder_token in _VARIABLE_PATTERNS:
        # placeholder_token is "{url}" / "{id}" / "{date}" — strip
        # the braces to get the bare key the recipe expects.
        key = placeholder_token.strip("{}")
        if key in out:
            continue
        m = pattern.search(request)
        if m is not None:
            out[key] = m.group(0)
    return out


def suggest_distillation(
    request: str,
    plan_task_types: Iterable[str],
    *,
    short_description: str = "",
) -> DistillationSuggestion:
    """Build a suggestion the REPL can show: 'save this as a skill named X?'

    Caller passes the plan's task_types (e.g. ["research", "analyze"])
    so the suggestion includes traceability about what the skill
    actually does.
    """
    return DistillationSuggestion(
        suggested_name=_suggested_name(request),
        template_seed=_template_seed(request),
        description=short_description or request[:120],
        subtask_signature=list(plan_task_types),
    )


# 0.1.45 — patterns that should NEVER be saved as skills regardless
# of how many times they repeat. "你好"+3 fires mining but isn't a
# workflow; a recipe of it is dead weight. List intentionally short
# — only obvious pleasantries / meta-asks. Longer wishlist goes in
# the "soft signal" section (multi-subtask, variable content, etc).
#
# Matched against normalized request (lowercased, punctuation stripped).
# Patterns are SUBSTRINGS not regex — keep it boring & fast.
_TRIVIAL_PATTERNS: tuple[str, ...] = (
    # English pleasantries
    "hi", "hello", "hey", "yo", "sup",
    "thanks", "thank you", "thx", "ty",
    "bye", "goodbye", "see you", "see ya",
    "ok", "okay", "k", "got it",
    "test", "ping", "ack",
    "how are you", "whats up", "what s up",
    # Chinese pleasantries (lowercase irrelevant for hanzi)
    "你好", "您好", "在吗", "在么", "在不在",
    "谢谢", "感谢", "thx", "多谢",
    "再见", "拜拜", "晚安", "早上好", "下午好", "晚上好",
    "好的", "好", "嗯", "嗯嗯", "收到",
    "测试", "测一下",
)


def is_trivial_request(request: str) -> bool:
    """0.1.45 — does the request look like a pleasantry / meta-ask
    that's never worth saving as a skill, no matter how many times
    it repeats?

    Strict match: the normalized request equals or is contained in
    one of the trivial patterns. We do NOT do substring-of-request
    (which would flag "你好 can you analyze X" as trivial). Instead
    we strip punctuation/whitespace and check if what's left IS the
    pattern. Bag-of-tokens (`你好 啊`) also matches because the
    normalized form drops whitespace.
    """
    # Lowercase + strip punctuation + collapse whitespace, same shape
    # as plan_cache.normalise_request. We want "你好！" and "你好 "
    # to both count as the literal greeting.
    import re as _re
    cleaned = _re.sub(r"[^\w]", "", request.lower())
    if not cleaned:
        return True  # empty request is the most trivial possible case
    for pat in _TRIVIAL_PATTERNS:
        pat_cleaned = _re.sub(r"[^\w]", "", pat.lower())
        if cleaned == pat_cleaned:
            return True
    return False


# 0.1.46 — signal weights for the skill-save scoring model. Each
# weight is "how many bits of evidence does firing this signal give
# us that this ask is worth saving as a skill?". Threshold
# `SAVE_SCORE_THRESHOLD` is what the sum has to clear to auto-save.
#
# Tuned so:
#   - refusal_retry alone (2.0) → save
#   - variable_content alone (1.5) → save
#   - diversity + depth or diversity + count → save
#   - any SINGLE weak signal alone → don't save
#
# The threshold sits at 1.5 so the weakest single-positive case is
# exactly a variable_content match — meaning if a request mentions
# a URL/ID/date, we save the workflow even without other signals.
# That's the "禅道 bug 56128 → 67890" case the user pointed at.
_SIGNAL_WEIGHTS: dict[str, float] = {
    "refusal_retry": 2.0,
    "variable_content": 1.5,
    "task_type_diversity": 1.0,
    "plan_depth_ge_2": 1.0,
    "subtask_count_ge_3": 0.8,
    "output_rich": 0.8,
    "request_long": 0.5,
}

SAVE_SCORE_THRESHOLD: float = 1.5


def _plan_depth(subtasks: list[object]) -> int:
    """Longest dependency chain in the DAG.

    A plan of [research, analyze(depends=research), synthesize(depends=analyze)]
    is depth 3 — a real pipeline. A plan of three parallel "general"
    subtasks is depth 1 — three parallel queries. The former is
    skill-worthy in a way the latter isn't.
    """
    # Build (task_type → depends_on names) lookup, taking the first
    # occurrence per task_type to keep this O(n).
    depmap: dict[str, list[str]] = {}
    for s in subtasks:
        tt = getattr(s, "task_type", None)
        deps = getattr(s, "depends_on", []) or []
        if isinstance(tt, str) and tt not in depmap:
            depmap[tt] = [d for d in deps if isinstance(d, str)]

    memo: dict[str, int] = {}

    def depth_of(name: str, visiting: set[str]) -> int:
        if name in memo:
            return memo[name]
        if name in visiting:
            # Cycle defense — depmap is user-derived, don't trust it.
            return 1
        visiting.add(name)
        parents = depmap.get(name, [])
        if not parents:
            d = 1
        else:
            d = 1 + max(depth_of(p, visiting) for p in parents)
        visiting.remove(name)
        memo[name] = d
        return d

    if not depmap:
        return 0
    return max(depth_of(name, set()) for name in depmap)


# Markdown / structural markers that indicate a "real report"-shaped
# output worth re-running on similar inputs. A 30-character text reply
# carries no structure; a multi-section report with headers, lists,
# and tables means the workflow produced something substantial.
_STRUCTURE_MARKERS = (
    "\n#",       # markdown header (with newline so we don't catch hashtags)
    "\n##",
    "\n- ",      # bulleted list
    "\n* ",
    "\n1.",      # numbered list
    "| ",        # table row separator (loose — false-positives on stylized prose)
    "```",       # code block
)


def _output_rich(text: str, *, min_length: int = 200) -> bool:
    """Does ``text`` look like a structured deliverable, not a one-line reply?

    Threshold pair: (length >= 200 chars) AND (>=2 structural markers).
    Either alone false-positives — a long flat paragraph is just chatty;
    a single `#` could be hashtag prose. Both together are a strong
    signal that the workflow produced a real artifact.
    """
    if len(text) < min_length:
        return False
    hits = sum(1 for m in _STRUCTURE_MARKERS if m in text)
    return hits >= 2


def skill_save_signals(
    request: str,
    *,
    plan_subtasks: Iterable[object] | None = None,
    had_refusal_retry: bool = False,
    final_output: str = "",
) -> dict[str, bool]:
    """0.1.46 — compute the diagnostic signal map for a candidate skill.

    Surfaces the SAME signals `worth_saving_as_skill` uses for its
    decision so callers (REPL `/skill explain`, tests, debug
    output) can see exactly which signals fired. Reasonable default
    values when info is missing (e.g. mining hint has no final_output).
    """
    subtasks = list(plan_subtasks) if plan_subtasks is not None else []
    task_types: set[str] = set()
    for s in subtasks:
        tt = getattr(s, "task_type", None)
        if isinstance(tt, str):
            task_types.add(tt)

    return {
        "refusal_retry": had_refusal_retry,
        "variable_content": any(
            p.search(request) for p, _ in _VARIABLE_PATTERNS
        ),
        "task_type_diversity": (
            len(task_types) >= 2
            or (len(task_types) == 1 and "general" not in task_types)
        ),
        "plan_depth_ge_2": _plan_depth(subtasks) >= 2,
        "subtask_count_ge_3": len(subtasks) >= 3,
        "output_rich": _output_rich(final_output),
        "request_long": len(request) >= 100,
    }


def worth_saving_as_skill(
    request: str,
    *,
    plan_subtasks: Iterable[object] | None = None,
    had_refusal_retry: bool = False,
    final_output: str = "",
) -> tuple[bool, str]:
    """0.1.45/0.1.46 — should this ask be saved as a reusable skill?

    Both the post-success auto-distillation (0.1.43) and the mining
    hint (0.1.17) route through here. They can't disagree on what
    "skill-worthy" means.

    0.1.45 implemented hard filters (trivial / single-subtask reject;
    refusal-retry / URL / task-diversity accept). 0.1.46 replaces the
    accept side with a *weighted score over 7 signals* so:

      - **Multiple weak signals can add up** to a save. E.g. a plan
        with depth>=2, diverse task_types, AND a long request was
        previously rejected (any single signal alone wasn't enough);
        now it scores 1.0 + 1.0 + 0.5 = 2.5 and saves correctly.
      - **Per-signal weights stay tunable** in one place
        (`_SIGNAL_WEIGHTS`) instead of being scattered through nested
        if-branches.
      - **Hard rejects stay hard** — trivial pattern and single-subtask
        plans still short-circuit before scoring. This is the
        "你好" guard that the user explicitly called out.

    Reason string carries the actual numbers so the REPL can say
    "saved (score 2.5: refusal_retry + diversity + length)" or
    "not saved (score 0.8: only request_long fired)" — surfacing
    judgment, not just verdict.
    """
    # Hard rejects — these are bright lines, not soft signals.
    if is_trivial_request(request):
        return False, "trivial pattern (greeting/pleasantry)"

    subtasks = list(plan_subtasks) if plan_subtasks is not None else []
    if len(subtasks) < 2:
        return False, "single-subtask asks aren't workflows"

    # Soft signals — compute then score.
    signals = skill_save_signals(
        request,
        plan_subtasks=subtasks,
        had_refusal_retry=had_refusal_retry,
        final_output=final_output,
    )
    fired = [name for name, val in signals.items() if val]
    score = sum(_SIGNAL_WEIGHTS[name] for name in fired)

    if score >= SAVE_SCORE_THRESHOLD:
        if not fired:
            # Defensive — shouldn't be reachable but mypy doesn't know.
            return True, f"score {score:.1f}"
        return True, f"score {score:.1f}: " + " + ".join(fired)

    if not fired:
        return False, "no positive signals"
    return (
        False,
        f"score {score:.1f} below threshold {SAVE_SCORE_THRESHOLD}: only "
        + " + ".join(fired)
        + " fired",
    )


def unique_slug(base: str, existing: Iterable[str]) -> str:
    """Pick a non-colliding slug. Returns ``base`` if free, else
    ``base-2``, ``base-3``, … until we find a fresh one.

    Pulled out so the REPL auto-save path and any future callers
    (e.g. `/skill rename`) share one collision policy.
    """
    seen = set(existing)
    if base not in seen:
        return base
    i = 2
    while f"{base}-{i}" in seen:
        i += 1
    return f"{base}-{i}"


def distill_request_to_recipe_fields(
    request: str,
    plan_subtasks: Iterable[tuple[str, str, Iterable[str]]],
    existing_names: Iterable[str],
) -> tuple[str, str, str, list[tuple[str, str, list[str]]]]:
    """Build the (name, template, description, subtasks) tuple for an
    auto-saved recipe — caller wraps it in ``Recipe`` / ``RecipeSubtask``.

    ``plan_subtasks`` is iter of (task_type, prompt, depends_on). We
    templatize each prompt with ``_template_seed`` so per-subtask
    URLs/IDs/dates don't lock the skill to one case.

    Decoupled from recipes.py so this module stays free of save/IO.
    Caller does the actual ``save_recipe()`` call.
    """
    # Materialize once — caller may pass an iterator.
    subs = list(plan_subtasks)
    sug = suggest_distillation(request, [tt for tt, _, _ in subs])
    slug = unique_slug(sug.suggested_name, existing_names)
    subtasks = [
        (tt, _template_seed(prompt), list(deps)) for tt, prompt, deps in subs
    ]
    return slug, sug.template_seed, request[:120], subtasks
