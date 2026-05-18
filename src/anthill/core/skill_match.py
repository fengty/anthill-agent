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
    try:
        recipes = list_recipes(nation_dir)
    except Exception:  # noqa: BLE001
        return None
    if not recipes:
        return None

    best: SkillMatch | None = None
    for recipe in recipes:
        scores = {
            "name": _cosine(_tokens(recipe.name), req_tokens),
            "description": _cosine(_tokens(recipe.description), req_tokens),
            "template": _cosine(_tokens(recipe.template), req_tokens),
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
