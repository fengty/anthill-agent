"""Recipes — name a request template, replay it with new arguments.

A user who runs "Research {topic} and write a one-page brief" once a
week shouldn't have to retype the wording every time, and shouldn't
have to pay Scout to re-plan the same workflow shape every time. A
Recipe captures both:
  - a parameterized request template (Python str.format placeholders)
  - the plan shape Scout produced the first time the recipe was run,
    so subsequent runs skip planning and go straight to execution

Recipes live as TOML under nations/<n>/recipes/<name>.toml — readable,
hand-editable, easy to grep. The TOML round-trip is intentionally
lossless: if the user opens the file and changes the template or
swaps a task_type, those edits are honored on the next run.

Two layers:
  - SimpleRecipe — template only. Scout still plans (using the
    cache on the formatted request), so re-runs with new arguments
    do produce fresh plans when the substitution materially changes
    the work. Cheaper than typing the request from scratch.
  - Explicit subtask list (the .subtasks field) — when present,
    Scout is skipped entirely and the saved plan is used as-is. This
    is the right escape hatch when the user has a workflow they want
    to be deterministic.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[import-not-found]

from anthill.core.scout import Plan, Subtask


@dataclass
class RecipeSubtask:
    """A pre-baked subtask in an explicit-plan recipe.

    Identical to scout.Subtask in shape, but kept distinct so the
    recipe layer can carry recipe-specific extensions later (e.g.
    per-subtask budget overrides) without touching Scout's surface.
    """

    task_type: str
    prompt_template: str
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Recipe:
    """A user-named, parameterized workflow.

    `template` is the request shown to Scout (and recorded in history)
    after argument substitution. `subtasks` is optional — when set,
    Scout is skipped and the subtask list runs directly with each
    subtask's prompt also substituted.
    """

    name: str
    template: str
    description: str = ""
    subtasks: list[RecipeSubtask] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_run_at: float | None = None
    run_count: int = 0

    def placeholders(self) -> list[str]:
        """Distinct {name} placeholders found in the template + any subtask prompts."""
        sources = [self.template] + [s.prompt_template for s in self.subtasks]
        found: list[str] = []
        seen: set[str] = set()
        for src in sources:
            for match in re.finditer(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", src):
                name = match.group(1)
                if name not in seen:
                    seen.add(name)
                    found.append(name)
        return found

    def fill(self, args: dict[str, str]) -> "FilledRecipe":
        """Substitute args into the template and any subtask prompts.

        Raises KeyError when a placeholder is missing — failing loudly
        is better than running the recipe with a literal '{topic}' in
        the prompt and wondering why the model is confused.
        """
        request = _safe_format(self.template, args)
        plan: Plan | None = None
        if self.subtasks:
            plan = Plan(
                subtasks=[
                    Subtask(
                        task_type=s.task_type,
                        prompt=_safe_format(s.prompt_template, args),
                        depends_on=list(s.depends_on),
                    )
                    for s in self.subtasks
                ]
            )
        return FilledRecipe(request=request, plan=plan, source=self)


@dataclass
class FilledRecipe:
    """The result of substituting arguments into a Recipe.

    Either `plan` is set (explicit recipe — skip Scout) or it isn't
    (simple template — Scout plans against `request`).
    """

    request: str
    plan: Plan | None
    source: Recipe


def _safe_format(template: str, args: dict[str, str]) -> str:
    """str.format that raises KeyError with a clear message on missing keys.

    Default str.format also raises KeyError but with no context — the
    user sees `KeyError: 'topic'` and has to guess what they typed
    wrong. We surface what was expected and what was provided.
    """
    try:
        return template.format(**args)
    except KeyError as e:
        missing = e.args[0] if e.args else "?"
        provided = ", ".join(sorted(args.keys())) or "(none)"
        raise KeyError(
            f"recipe placeholder {{{missing}}} not provided "
            f"(arguments given: {provided})"
        ) from None


# --- on-disk I/O -----------------------------------------------------------

def recipes_dir(nation_dir_path: Path) -> Path:
    return nation_dir_path / "recipes"


def recipe_path(nation_dir_path: Path, name: str) -> Path:
    return recipes_dir(nation_dir_path) / f"{_sanitize_name(name)}.toml"


def _sanitize_name(name: str) -> str:
    """Restrict to a filesystem-safe shape. Whitelist over blacklist."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", name).strip("_")
    if not cleaned:
        raise ValueError(f"recipe name {name!r} contains no usable characters")
    return cleaned


def save_recipe(recipe: Recipe, nation_dir_path: Path) -> Path:
    """Write the recipe as a hand-editable TOML file."""
    target = recipe_path(nation_dir_path, recipe.name)
    target.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = [
        f'name = "{_escape(recipe.name)}"',
        f'template = "{_escape(recipe.template)}"',
    ]
    if recipe.description:
        lines.append(f'description = "{_escape(recipe.description)}"')
    lines.append(f"created_at = {recipe.created_at}")
    if recipe.last_run_at is not None:
        lines.append(f"last_run_at = {recipe.last_run_at}")
    lines.append(f"run_count = {recipe.run_count}")

    for sub in recipe.subtasks:
        lines.append("")
        lines.append("[[subtasks]]")
        lines.append(f'task_type = "{_escape(sub.task_type)}"')
        lines.append(f'prompt_template = "{_escape(sub.prompt_template)}"')
        if sub.depends_on:
            deps = ", ".join(f'"{_escape(d)}"' for d in sub.depends_on)
            lines.append(f"depends_on = [{deps}]")
        else:
            lines.append("depends_on = []")

    target.write_text("\n".join(lines) + "\n")
    return target


def _escape(s: str) -> str:
    """TOML basic-string escaping for the small subset we emit."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def load_recipe(name: str, nation_dir_path: Path) -> Recipe | None:
    path = recipe_path(nation_dir_path, name)
    if not path.exists():
        return None
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return _recipe_from_dict(data)


def list_recipes(nation_dir_path: Path) -> list[Recipe]:
    """Every readable recipe, alphabetical by name."""
    d = recipes_dir(nation_dir_path)
    if not d.exists():
        return []
    out: list[Recipe] = []
    for path in sorted(d.glob("*.toml")):
        try:
            data = tomllib.loads(path.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            continue
        recipe = _recipe_from_dict(data)
        if recipe is not None:
            out.append(recipe)
    return out


def _recipe_from_dict(data: dict) -> Recipe | None:
    name = data.get("name")
    template = data.get("template")
    if not isinstance(name, str) or not isinstance(template, str):
        return None
    subs_raw = data.get("subtasks", [])
    subs: list[RecipeSubtask] = []
    if isinstance(subs_raw, list):
        for s in subs_raw:
            if not isinstance(s, dict):
                continue
            tt = s.get("task_type")
            pt = s.get("prompt_template")
            if not isinstance(tt, str) or not isinstance(pt, str):
                continue
            deps = s.get("depends_on", []) or []
            subs.append(
                RecipeSubtask(
                    task_type=tt,
                    prompt_template=pt,
                    depends_on=[d for d in deps if isinstance(d, str)],
                )
            )
    return Recipe(
        name=name,
        template=template,
        description=str(data.get("description", "")),
        subtasks=subs,
        created_at=float(data.get("created_at", time.time())),
        last_run_at=float(data["last_run_at"]) if "last_run_at" in data else None,
        run_count=int(data.get("run_count", 0)),
    )


def remove_recipe(name: str, nation_dir_path: Path) -> bool:
    path = recipe_path(nation_dir_path, name)
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def record_run(recipe: Recipe, nation_dir_path: Path) -> None:
    """Bump run_count + last_run_at after a successful run."""
    recipe.last_run_at = time.time()
    recipe.run_count += 1
    save_recipe(recipe, nation_dir_path)


__all__ = [
    "Recipe",
    "RecipeSubtask",
    "FilledRecipe",
    "save_recipe",
    "load_recipe",
    "list_recipes",
    "remove_recipe",
    "record_run",
    "recipe_path",
]
