#!/usr/bin/env python3
"""Сборка самодостаточных промптов, таблиц README и страницы GitHub Pages из src/.

  python3 scripts/build.py                собрать prompts/ и таблицы README
  python3 scripts/build.py --check        проверить, что все собрано из src/, и прогнать проверки
  python3 scripts/build.py --site _site   дополнительно собрать страницу в каталог _site
"""
import argparse
import html
import json
import os
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PROMPTS = ROOT / "prompts"
README = ROOT / "README.md"
REPO = "snaplyze/agent-playbook"
PARAMS, RULES = "{{ПАРАМЕТРЫ}}", "{{ПРАВИЛА}}"
PROMPT_LIMIT = 20000  # символов: промпт отправляется одним сообщением
# Пустое обязательное значение: агент должен спросить, а не взять пример из подсказки поля.
MISSING = "<не указано: спроси владельца, не угадывай>"

# Промпты описывают возможности по сути: без названий клиентов, расширений и версий.
FORBIDDEN = [
    r"\b(codex|claude|cursor|gemini|copilot|windsurf|aider|cline|roo|jules|zed|openai|anthropic|gpt)\b",
    r"\borchestration:", r"\$[a-z][\w-]+", r"create_goal", r"(?<![\w/-])/goal\b",
    r"policy-version", r"unified-agent-policy", r"\bv\d+\b",
    r"\b\d+\.\d+\.\d{1,3}\b",  # номер версии; даты с годом из 4 цифр не совпадают
    r"редакци\w*\s+\d", r"плагин",
]


def fail(message):
    sys.exit(f"build: {message}")


# --- Модель: сценарии, параметры, сборка текста -------------------------------------------------

def load():
    data = tomllib.loads((SRC / "scenarios.toml").read_text(encoding="utf-8"))
    rules = (SRC / "rules.md").read_text(encoding="utf-8").strip()
    listed = {item["file"] for item in data["scenario"]}
    present = {path.name for path in (SRC / "scenarios").glob("*.md")}
    if listed != present:
        fail(f"src/scenarios/ и scenarios.toml расходятся: {sorted(listed ^ present)}")
    scenarios = []
    for item in data["scenario"]:
        source = (SRC / "scenarios" / item["file"]).read_text(encoding="utf-8")
        for token in (PARAMS, RULES):
            if source.count(token) != 1:
                fail(f"src/scenarios/{item['file']}: {token} должен встречаться ровно один раз")
        params = item.get("param", [])
        for param in params:
            validate(item["file"], param)
        if len({param["key"] for param in params}) != len(params):
            fail(f"{item['file']}: повторяются имена параметров")
        scenarios.append(dict(item, id=Path(item["file"]).stem.split("-", 1)[1],
                              num=item["file"][:2], text=source.replace(RULES, rules), params=params))
    return scenarios


def validate(file, param):
    where = f"{file}, параметр «{param['key']}»"
    if "choices" in param:
        if param.get("default") not in ("all", "none") or not all(param.get(k) for k in ("all", "none", "some")):
            fail(f"{where}: нужны default = all|none и тексты all, none, some")
        return
    if "input" in param.get("option", [{}])[0]:
        fail(f"{where}: первый вариант — значение по умолчанию, он не может требовать ввода")
    for option in param.get("option") or fail(f"{where}: нет вариантов"):
        if ("{input}" in option["line"]) != ("input" in option):
            fail(f"{where}, «{option['label']}»: {{input}} и поле input должны быть вместе")
        if ("{choice}" in option["line"]) != ("choice" in option):
            fail(f"{where}, «{option['label']}»: {{choice}} и поле choice должны быть вместе")


def default_state(param):
    if "choices" in param:
        return {"picked": list(param["choices"]) if param["default"] == "all" else []}
    return {"index": 0}


def param_text(param, state):
    """Строка параметра в промпте. Та же логика повторена в paramText на странице."""
    if "choices" in param:
        selected = [c for c in param["choices"] if c in state["picked"]]
        rest = [c for c in param["choices"] if c not in selected]
        template = param["none"] if not selected else param["all"] if not rest else param["some"]
        return (template.replace("{all}", ", ".join(param["choices"]))
                .replace("{selected}", ", ".join(selected)).replace("{rest}", ", ".join(rest)))
    option = param["option"][state["index"]]
    text = option["line"]
    if "input" in option:
        text = text.replace("{input}", " ".join(state.get("input", "").split()) or MISSING)
    if "choice" in option:
        text = text.replace("{choice}", state.get("choice") or option["choice"][0])
    return text


def params_block(scenario, states=None):
    if not scenario["params"]:
        return ""
    states = states or [default_state(param) for param in scenario["params"]]
    lines = [f"- {param['key']}: {param_text(param, state)}" for param, state in zip(scenario["params"], states)]
    return "## Параметры\n" + "\n".join(lines)


def split(scenario):
    """Текст до и после раздела параметров; страница собирает промпт так же."""
    if not scenario["params"]:
        return scenario["text"].replace(PARAMS + "\n\n", ""), ""
    return scenario["text"].split(PARAMS)


def assemble(scenario, states=None):
    before, after = split(scenario)
    return before + params_block(scenario, states) + after


def variant_states(scenario):
    """Каждый вариант каждого параметра (с заполненным и пустым значением) при остальных по умолчанию."""
    defaults = [default_state(param) for param in scenario["params"]]
    for i, param in enumerate(scenario["params"]):
        if "choices" in param:
            options = [[], param["choices"], param["choices"][:1], param["choices"][1:]]
            variants = [{"picked": picked} for picked in options]
        else:
            variants = []
            for index, option in enumerate(param["option"]):
                inputs = ["  значение   $& из поля ", ""] if "input" in option else [""]
                for choice in option.get("choice", [""]):
                    variants += [{"index": index, "input": value, "choice": choice} for value in inputs]
        for variant in variants:
            yield defaults[:i] + [variant] + defaults[i + 1:]
    if not scenario["params"]:
        yield []


def parity_cases(scenarios):
    return [{"id": s["id"], "states": states, "expected": assemble(s, states or None)}
            for s in scenarios for states in variant_states(s)]


# --- README: таблицы между маркерами -----------------------------------------------------------

def readme_tables(scenarios):
    rows = ["| № | Промпт | Когда | Результат | Чего не делает | Символов |", "|---|---|---|---|---|---:|"]
    rows += [f"| {s['num']} | [{s['name']}](prompts/{s['file']}) | {s['when']} | {s['result']} | {s['never']} "
             f"| {len(assemble(s)):,} |".replace(",", "\u00a0") for s in scenarios]
    params = ["| Промпт | Параметр | Варианты |", "|---|---|---|"]
    for s in scenarios:
        for param in s["params"]:
            if "choices" in param:
                default = "все отмечены" if param["default"] == "all" else "ничего не отмечено"
                variants = f"любые из: {', '.join(param['choices'])} (по умолчанию {default})"
            else:
                labels = [o["label"].rstrip("…") + (" — со своим значением" if "input" in o else "")
                          for o in param["option"]]
                variants = f"{labels[0]} (по умолчанию); " + "; ".join(labels[1:])
            params.append(f"| {s['num']} {s['name']} | {param['key']} | {variants} |")
    return {"scenarios": "\n".join(rows), "params": "\n".join(params)}


def render_readme(text, tables):
    for name, table in tables.items():
        pattern = rf"(<!-- {name}:begin -->\n)(?:.*?\n)?(<!-- {name}:end -->)"
        if not re.search(pattern, text, re.S):
            fail(f"README: нет маркеров {name}:begin / {name}:end")
        text = re.sub(pattern, lambda m: m.group(1) + table + "\n" + m.group(2), text, count=1, flags=re.S)
    return text


# --- Проверки ----------------------------------------------------------------------------------

def check(scenarios, built, readme):
    errors = []
    expected = {f"prompts/{s['file']}": text for s, text in zip(scenarios, built)}
    actual = {f"prompts/{p.name}": p.read_text(encoding="utf-8") for p in PROMPTS.glob("*.md")}
    for path in sorted(expected.keys() | actual.keys()):
        if expected.get(path) != actual.get(path):
            errors.append(f"{path}: не совпадает со сборкой из src/ — запусти python3 scripts/build.py")
    if README.read_text(encoding="utf-8") != readme:
        errors.append("README.md: таблицы не совпадают со сборкой — запусти python3 scripts/build.py")
    sources = [(f"prompts/{s['file']}", text) for s, text in zip(scenarios, built)]
    sources += [(str(p.relative_to(ROOT)), p.read_text(encoding="utf-8")) for p in SRC.rglob("*") if p.is_file()]
    for name, text in sources:
        for pattern in FORBIDDEN:
            for match in re.finditer(pattern, text, re.I):
                line = text.count("\n", 0, match.start()) + 1
                errors.append(f"{name}:{line}: недопустимое упоминание «{match.group(0)}»")
    for s in scenarios:
        for states in variant_states(s):
            text = assemble(s, states or None)
            if re.search(r"\{[a-zа-яё_]+\}|\{\{|\}\}", text):
                errors.append(f"{s['file']}: незаполненная метка при вариантах {states}")
            if len(text) > PROMPT_LIMIT:
                errors.append(f"{s['file']}: {len(text)} символов при вариантах {states}, предел {PROMPT_LIMIT}")
    for s, text in zip(scenarios, built):
        if len(text) > PROMPT_LIMIT:
            errors.append(f"prompts/{s['file']}: {len(text)} символов, предел {PROMPT_LIMIT}")
        if "{{" in text or "}}" in text:
            errors.append(f"prompts/{s['file']}: осталась неподставленная метка")
    for doc in ("README.md", "AGENTS.md"):
        for target in re.findall(r"\]\(<?([^)#\s>]+)", (ROOT / doc).read_text(encoding="utf-8")):
            if not re.match(r"[a-z]+:", target) and not (ROOT / target).exists():
                errors.append(f"{doc}: битая ссылка {target}")
    return errors


# --- Страница ----------------------------------------------------------------------------------

def inline(text):
    text = html.escape(text, quote=False)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", text)


def param_field(scenario, index, param):
    fid = f"{scenario['id']}-{index}"
    esc = html.escape
    help_html = f'<span class="help" id="{fid}-help">{inline(param["help"])}</span>'
    if "choices" in param:
        checked = " checked" if param["default"] == "all" else ""
        choices = "".join(f'<label class="choice"><input type="checkbox" value="{esc(c)}"{checked}> {esc(c)}</label>'
                          for c in param["choices"])
        return (f'<div class="field wide" data-index="{index}" role="group" aria-labelledby="{fid}-label" '
                f'aria-describedby="{fid}-help"><span class="label" id="{fid}-label">{esc(param["key"])}</span>'
                f'<div class="choices">{choices}</div>{help_html}</div>')
    options = "".join(f'<option value="{i}">{esc(o["label"])}</option>' for i, o in enumerate(param["option"]))
    extra = ""
    if any("input" in o for o in param["option"]):
        extra += (f'<input type="text" class="extra-input" hidden autocomplete="off" spellcheck="false" '
                  f'aria-label="{esc(param["key"])}: значение">')
    if any("choice" in o for o in param["option"]):
        extra += f'<select class="extra-choice" hidden aria-label="{esc(param["key"])}: область"></select>'
    extra = f'<div class="extra">{extra}</div>' if extra else ""
    return (f'<div class="field" data-index="{index}"><label for="{fid}">{esc(param["key"])}</label>'
            f'<select id="{fid}" aria-describedby="{fid}-help">{options}</select>{extra}{help_html}</div>')


def scenario_section(scenario, built):
    fields = "".join(param_field(scenario, i, p) for i, p in enumerate(scenario["params"]))
    params = f'<fieldset class="params"><legend>Параметры</legend>{fields}</fieldset>' if fields else ""
    sid = scenario["id"]
    return f"""
<section class="scenario" id="{sid}" aria-labelledby="{sid}-title" data-file="{scenario['file']}">
  <h2 id="{sid}-title"><span class="num">{scenario['num']}</span> {html.escape(scenario['name'])}</h2>
  <dl class="facts">
    <div><dt>Когда</dt><dd>{inline(scenario['when'])}</dd></div>
    <div><dt>Результат</dt><dd>{inline(scenario['result'])}</dd></div>
    <div><dt>Чего не делает</dt><dd>{inline(scenario['never'])}</dd></div>
  </dl>
  {params}
  <div class="bar">
    <button type="button" class="primary" data-action="copy">Скопировать промпт</button>
    <button type="button" class="secondary" data-action="download">Скачать .md</button>
    <span class="meta" aria-live="polite"></span>
  </div>
  <pre class="prompt" tabindex="0" aria-label="Текст промпта">{html.escape(built, quote=False)}</pre>
</section>"""


def build_site(scenarios, built, out):
    data = [{"id": s["id"], "file": s["file"], "before": split(s)[0], "after": split(s)[1], "params": s["params"]}
            for s in scenarios]
    rail = "".join(f'<li><a href="#{s["id"]}" data-id="{s["id"]}"><span class="num">{s["num"]}</span>'
                   f'<span class="name">{html.escape(s["name"])}</span></a></li>' for s in scenarios)
    sha = os.environ.get("GITHUB_SHA", "")[:7]
    built_from = f' Собрано из коммита <a href="https://github.com/{REPO}/commit/{sha}">{sha}</a>.' if sha else ""
    page = (TEMPLATE.replace("@RAIL@", rail)
            .replace("@SECTIONS@", "".join(scenario_section(s, t) for s, t in zip(scenarios, built)))
            .replace("@DATA@", json.dumps({"missing": MISSING, "scenarios": data}, ensure_ascii=False).replace("</", "<\\/"))
            .replace("@REPO@", REPO).replace("@BUILT@", built_from))
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    print(f"{out / 'index.html'}: {len(page.encode())} байт")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="только проверить, ничего не записывая")
    parser.add_argument("--site", type=Path, help="каталог для страницы GitHub Pages")
    parser.add_argument("--parity-cases", type=Path, help="записать случаи для scripts/parity.mjs")
    args = parser.parse_args()
    scenarios = load()
    built = [assemble(s) for s in scenarios]
    readme = render_readme(README.read_text(encoding="utf-8"), readme_tables(scenarios))
    if args.check:
        errors = check(scenarios, built, readme)
        for s, text in zip(scenarios, built):
            print(f"prompts/{s['file']}: {len(text)} символов")
        if errors:
            sys.stdout.flush()
            print("\n".join(["", "ОШИБКИ:", *errors]), file=sys.stderr)
            sys.exit(1)
        print("OK")
    else:
        PROMPTS.mkdir(exist_ok=True)
        for stale in set(PROMPTS.glob("*.md")) - {PROMPTS / s["file"] for s in scenarios}:
            stale.unlink()
        for s, text in zip(scenarios, built):
            (PROMPTS / s["file"]).write_text(text, encoding="utf-8")
        README.write_text(readme, encoding="utf-8")
        print(f"собрано: {len(built)} промптов, таблицы README")
    if args.site:
        build_site(scenarios, built, args.site)
    if args.parity_cases:
        args.parity_cases.write_text(json.dumps(parity_cases(scenarios), ensure_ascii=False), encoding="utf-8")


TEMPLATE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent-playbook</title>
<meta name="description" content="Самодостаточные промпты для AI-агентов, которые работают с кодом: выберите сценарий и параметры, скопируйте готовый текст.">
<meta property="og:title" content="agent-playbook">
<meta property="og:description" content="Самодостаточные промпты для AI-агентов, которые работают с кодом.">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect x='3' y='3' width='26' height='26' rx='5' fill='none' stroke='%235B3FA8' stroke-width='3'/%3E%3Crect x='9' y='9' width='14' height='14' rx='2' fill='%235B3FA8'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Golos+Text:wght@400;500;600&family=JetBrains+Mono:wght@400;600&family=PT+Serif:wght@700&display=swap">
<style>
:root {
  --paper: #F2F4F3;
  --sheet: #FFFFFF;
  --ink: #1B2333;
  --ink-soft: #4E5767;
  --rule: #D3D8DD;
  --accent: #5B3FA8;
  --accent-wash: #ECE7F8;
  --on-ink: #FFFFFF;
  --code-ink: #2F5F4F;
  --serif: "PT Serif", Georgia, serif;
  --sans: "Golos Text", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --mono: "JetBrains Mono", ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --paper: #11151D; --sheet: #181D27; --ink: #E4E7EC; --ink-soft: #A0A8B6; --rule: #2B3240;
    --accent: #B39CFF; --accent-wash: #2A2440; --on-ink: #11151D; --code-ink: #8CCFB8;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --paper: #11151D; --sheet: #181D27; --ink: #E4E7EC; --ink-soft: #A0A8B6; --rule: #2B3240;
  --accent: #B39CFF; --accent-wash: #2A2440; --on-ink: #11151D; --code-ink: #8CCFB8;
  color-scheme: dark;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--paper); color: var(--ink); font: 400 16px/1.5 var(--sans); }
a { color: inherit; text-decoration-color: var(--accent); text-underline-offset: 3px; }
a:hover { color: var(--accent); }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
code { font: 0.86em/1.4 var(--mono); background: var(--sheet); border: 1px solid var(--rule); border-radius: 3px; padding: 0 0.25em; -webkit-box-decoration-break: clone; box-decoration-break: clone; }

.wrap { max-width: 1160px; margin: 0 auto; padding: 0 16px; }
.top { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline; gap: 4px 16px; padding: 20px 0 0; font-size: 15px; color: var(--ink-soft); }
.top strong, .top a { white-space: nowrap; }
.top strong { color: var(--ink); font-weight: 600; }
.intro { padding: 44px 0 36px; max-width: 46rem; }
.intro h1 { font: 700 clamp(1.9rem, 4.6vw, 3rem)/1.1 var(--serif); letter-spacing: -0.015em; margin: 0 0 18px; text-wrap: balance; }
.intro p { font-size: 1.125rem; line-height: 1.55; color: var(--ink-soft); margin: 0; max-width: 42rem; }

.layout { display: grid; grid-template-columns: clamp(176px, 17vw, 210px) minmax(0, 1fr); gap: clamp(28px, 4vw, 48px); align-items: start; padding-bottom: 64px; }
.rail { position: sticky; top: 24px; }
.rail ul { list-style: none; margin: 0; padding: 0; border-left: 1px solid var(--rule); }
.rail a { display: flex; gap: 12px; align-items: baseline; min-height: 44px; padding: 10px 0 10px 16px; margin-left: -1px; border-left: 3px solid transparent; text-decoration: none; color: var(--ink-soft); }
.rail a:hover { color: var(--ink); }
.rail a[aria-current="page"] { color: var(--ink); border-left-color: var(--accent); font-weight: 500; }
.num { font-variant-numeric: tabular-nums; color: var(--accent); font-weight: 500; }
.rail .order { margin: 20px 0 0; padding-left: 16px; font-size: 14px; line-height: 1.5; color: var(--ink-soft); }

.scenario { scroll-margin-top: 16px; }
.scenario + .scenario { margin-top: 72px; }
.js .scenario + .scenario { margin-top: 0; }
.scenario h2 { font: 700 2rem/1.15 var(--serif); margin: 0 0 20px; letter-spacing: -0.01em; }
.scenario h2 .num { font: 500 1.1rem/1 var(--sans); margin-right: 6px; vertical-align: 0.35em; }
.facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 190px), 1fr)); gap: 16px 28px; margin: 0 0 28px; max-width: 60rem; }
.facts dt { font-size: 14px; color: var(--ink-soft); margin-bottom: 4px; }
.facts dd { margin: 0; line-height: 1.45; }

.params { border: 1px solid var(--rule); border-radius: 8px; background: var(--sheet); padding: 18px 20px 20px; margin: 0 0 20px; display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 240px), 1fr)); gap: 18px 24px; }
.params legend { font: 600 15px/1 var(--sans); padding: 0 6px; margin-left: -6px; color: var(--ink-soft); }
.field { display: grid; gap: 6px; align-content: start; min-width: 0; }
.field > label, .field .label { font-weight: 500; }
.field.wide { grid-column: 1 / -1; }
.field select, .field input[type="text"] { font: 400 16px/1.4 var(--sans); color: var(--ink); background: var(--paper); border: 1px solid var(--rule); border-radius: 6px; padding: 10px 12px; min-height: 44px; width: 100%; min-width: 0; }
.field input[type="text"] { font-family: var(--mono); }
.field select:focus, .field input[type="text"]:focus { border-color: var(--accent); outline: 2px solid var(--accent-wash); outline-offset: 0; }
.field input[aria-invalid="true"] { border-color: var(--accent); }
.extra { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; }
.extra:not(:has(:not([hidden]))) { display: none; }
.extra > [hidden] { display: none; }
.extra > .extra-choice { width: auto; }
.extra > :only-child, .extra:not(:has(.extra-choice:not([hidden]))) > .extra-input { grid-column: 1 / -1; }
.choices { display: flex; flex-wrap: wrap; gap: 2px 22px; }
.choice { display: flex; flex: 0 1 auto; max-width: 100%; gap: 10px; align-items: center; min-height: 40px; line-height: 1.3; overflow-wrap: anywhere; cursor: pointer; }
.choice input { width: 18px; height: 18px; margin: 0; flex: none; accent-color: var(--accent); }
.help { font-size: 14px; line-height: 1.45; color: var(--ink-soft); }

.bar { position: sticky; top: 0; z-index: 2; display: flex; flex-wrap: wrap; gap: 10px; align-items: center; padding: 10px 0; margin-bottom: 12px; background: var(--paper); border-bottom: 1px solid var(--rule); }
button { font: 500 15px/1.2 var(--sans); border-radius: 6px; padding: 11px 16px; min-height: 44px; cursor: pointer; border: 1px solid var(--ink); }
button.primary { background: var(--ink); color: var(--on-ink); min-width: 13.5em; }
button.primary:hover { background: var(--accent); border-color: var(--accent); }
button.secondary { background: transparent; color: var(--ink); }
button.secondary:hover { border-color: var(--accent); color: var(--accent); }
.meta { font-size: 14px; color: var(--ink-soft); margin-left: auto; }
.js .bar { display: flex; }
.bar { display: none; }
html:not(.js) .params { display: none; }

.prompt { margin: 0; padding: clamp(16px, 3vw, 28px); background: var(--sheet); border: 1px solid var(--rule); border-radius: 8px; font: 400 14px/1.7 var(--mono); white-space: pre-wrap; overflow-wrap: anywhere; tab-size: 2; }
.prompt .h { font-weight: 600; color: var(--ink); }
.prompt .m { color: var(--accent); }
.prompt .c { color: var(--code-ink); }
.prompt .b { font-weight: 600; }
.prompt .p { background: var(--accent-wash); box-shadow: 0 0 0 2px var(--accent-wash); border-radius: 2px; }
.prompt .p.changed { animation: changed 1.2s ease-out; }
@keyframes changed { from { background: var(--accent); color: var(--on-ink); } to { background: var(--accent-wash); } }

footer { border-top: 1px solid var(--rule); padding: 24px 0 40px; font-size: 14px; color: var(--ink-soft); }
footer p { margin: 0; max-width: 46rem; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }

@media (max-width: 860px) {
  .intro { padding: 32px 0 28px; }
  .layout { grid-template-columns: minmax(0, 1fr); gap: 32px; }
  .rail { position: static; }
  .rail ul { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; border-left: 0; }
  .rail a { flex-direction: column; align-items: flex-start; gap: 2px; height: 100%; margin: 0; padding: 8px 12px 10px; border: 1px solid var(--rule); border-radius: 6px; background: var(--sheet); font-size: 15px; line-height: 1.3; }
  .rail .num { font-size: 13px; }
  .rail a[aria-current="page"] { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
  .rail .order { padding-left: 0; margin-top: 12px; }
  .scenario h2 { font-size: 1.65rem; }
}
@media (max-width: 739px) { .rail ul { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 520px) {
  .top { padding-top: 16px; }
  .intro { padding: 24px 0 20px; }
  .intro h1 { font-size: clamp(1.7rem, 8.2vw, 2.2rem); margin-bottom: 14px; }
  .intro p { font-size: 1rem; }
  .layout { gap: 28px; }
  .rail ul { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .rail li:last-child:nth-child(odd) { grid-column: 1 / -1; }
  .scenario h2 { font-size: 1.5rem; }
  .params { padding: 14px 14px 16px; }
  .extra { grid-template-columns: minmax(0, 1fr); }
  .extra > .extra-choice { width: 100%; }
  .choices { gap: 0 18px; }
  .choice { font-size: 15px; }
  .bar button { flex: 1 1 auto; }
  button.primary { min-width: 0; }
  .meta { width: 100%; margin-left: 0; }
  .prompt { font-size: 13px; line-height: 1.65; padding: 14px; }
}
@media (prefers-reduced-motion: reduce) { .prompt .p.changed { animation: none; } }
</style>
</head>
<body>
<div class="wrap">
  <div class="top"><strong>agent-playbook</strong><a href="https://github.com/@REPO@">Репозиторий на GitHub</a></div>
  <header class="intro">
    <h1>Промпты для AI-агентов, которые работают с кодом</h1>
    <p>Каждый промпт самодостаточен: в нем цель, общие правила и шаги сценария, поэтому он работает в любом агентном клиенте без подготовки. Выберите сценарий и параметры — текст ниже обновится сразу, — затем скопируйте его и отправьте агенту одним сообщением.</p>
  </header>
  <div class="layout">
    <nav class="rail" aria-label="Промпты">
      <ul>@RAIL@</ul>
      <p class="order">Обычно запускают 01, затем 03. 00 — по желанию, один раз в проекте; 02 — когда разросся план; 04 — когда нужно доставить весь согласованный состав.</p>
    </nav>
    <main>@SECTIONS@</main>
  </div>
  <footer><p>Промпты собраны из каталога src/ репозитория <a href="https://github.com/@REPO@">@REPO@</a>, лицензия Apache-2.0.@BUILT@</p></footer>
</div>
<p class="sr-only" role="status" id="status"></p>
<script type="application/json" id="data">@DATA@</script>
<script id="logic">
// Сборка текста промпта: та же логика, что param_text и assemble в scripts/build.py.
// CI сверяет их на всех вариантах параметров (scripts/parity.mjs).
var playbook = (() => {
  const clean = (value) => value.trim().replace(/\s+/g, " ");
  // Замена функцией: строка замены разбирала бы шаблоны вида $& во вводе.
  const put = (text, token, value) => text.replaceAll(token, () => value);
  function paramText(param, state, missing) {
    if (param.choices) {
      const selected = param.choices.filter((c) => state.picked.includes(c));
      const rest = param.choices.filter((c) => !selected.includes(c));
      const template = !selected.length ? param.none : !rest.length ? param.all : param.some;
      return put(put(put(template, "{all}", param.choices.join(", ")), "{selected}", selected.join(", ")), "{rest}", rest.join(", "));
    }
    const option = param.option[state.index];
    let text = option.line;
    if (option.input !== undefined) text = put(text, "{input}", clean(state.input || "") || missing);
    if (option.choice) text = put(text, "{choice}", state.choice || option.choice[0]);
    return text;
  }
  function compose(model, states, missing) {
    const lines = model.params.map((param, i) => "- " + param.key + ": " + paramText(param, states[i], missing));
    return { lines, text: model.before + (lines.length ? "## Параметры\n" + lines.join("\n") : "") + model.after };
  }
  return { clean, compose };
})();
</script>
<script>
(() => {
  document.documentElement.classList.add("js");
  const { scenarios: data, missing } = JSON.parse(document.getElementById("data").textContent);
  const { clean, compose } = playbook;
  const status = document.getElementById("status");
  const sections = [...document.querySelectorAll(".scenario")];
  const links = [...document.querySelectorAll(".rail a[data-id]")];
  const esc = (value) => value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const plural = (n) => n % 10 === 1 && n % 100 !== 11 ? "символ" : [2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100) ? "символа" : "символов";

  function highlight(text, marked, changed) {
    return text.split("\n").map((line) => {
      let out = esc(line);
      if (/^#{1,6} /.test(line)) return '<span class="h">' + out + "</span>";
      out = out.replace(/^(\s*)(- |\d+\. )/, '$1<span class="m">$2</span>');
      out = out.replace(/`([^`]+)`/g, '<span class="c">`$1`</span>');
      out = out.replace(/\*\*(.+?)\*\*/g, '<span class="b">**$1**</span>');
      if (marked.has(line)) out = '<span class="p' + (changed.has(line) ? " changed" : "") + '">' + out + "</span>";
      return out;
    }).join("\n");
  }

  function show(id, scroll) {
    sections.forEach((section) => { section.hidden = section.id !== id; });
    links.forEach((link) => link.dataset.id === id ? link.setAttribute("aria-current", "page") : link.removeAttribute("aria-current"));
    const section = document.getElementById(id);
    if (scroll && section.getBoundingClientRect().top < 0) section.scrollIntoView({ block: "start" });
  }
  const current = () => sections.some((s) => s.id === location.hash.slice(1)) ? location.hash.slice(1) : sections[0].id;
  links.forEach((link) => link.addEventListener("click", (event) => {
    event.preventDefault();
    history.replaceState(null, "", "#" + link.dataset.id);
    show(link.dataset.id, true);
  }));
  window.addEventListener("hashchange", () => show(current(), true));
  show(current(), false);
  // Браузер прокрутил к якорю до того, как скрипт спрятал остальные промпты: выравниваем заново.
  if (location.hash) document.getElementById(current()).scrollIntoView({ block: "start" });

  sections.forEach((section) => {
    const model = data.find((item) => item.id === section.id);
    const pre = section.querySelector(".prompt");
    const meta = section.querySelector(".meta");
    const fields = [...section.querySelectorAll(".field[data-index]")];
    let text = "", previous = new Set();

    // Состояние элементов управления: видимость поля значения и второго выбора.
    function state(field) {
      const param = model.params[field.dataset.index];
      if (param.choices) return { picked: [...field.querySelectorAll("input:checked")].map((input) => input.value) };
      const index = Number(field.querySelector("select").value);
      const option = param.option[index];
      const input = field.querySelector(".extra-input");
      const choice = field.querySelector(".extra-choice");
      if (input) {
        input.hidden = option.input === undefined;
        input.placeholder = option.input || "";
        input.setAttribute("aria-invalid", String(option.input !== undefined && !clean(input.value)));
      }
      if (choice) {
        choice.hidden = !option.choice;
        if (option.choice && choice.dataset.for !== String(index)) {
          choice.innerHTML = option.choice.map((value) => "<option>" + esc(value) + "</option>").join("");
          choice.dataset.for = String(index);
        }
      }
      return { index, input: input ? input.value : "", choice: choice && !choice.hidden ? choice.value : "" };
    }

    function render() {
      const result = compose(model, fields.map(state), missing);
      const lines = result.lines;
      text = result.text;
      const marked = new Set(lines);
      const changed = new Set(previous.size ? lines.filter((line) => !previous.has(line)) : []);
      previous = marked;
      pre.innerHTML = highlight(text, marked, changed);
      meta.textContent = text.length.toLocaleString("ru-RU") + " " + plural(text.length);
    }
    section.addEventListener("input", render);
    section.addEventListener("change", (event) => {
      render();
      const input = event.target.matches("select:not(.extra-choice)") && event.target.closest(".field").querySelector(".extra-input:not([hidden])");
      if (input && !clean(input.value)) input.focus();
    });
    render();

    // Пустое обязательное значение: не отдаем промпт с меткой «не указано», а ведем к полю.
    function blocked(button) {
      const input = [...section.querySelectorAll('.extra-input:not([hidden])')].find((field) => !clean(field.value));
      if (!input) return false;
      done(button, "Заполните: " + input.closest(".field").querySelector("label").textContent);
      input.focus();
      return true;
    }
    function done(button, label) {
      const original = button.dataset.label || (button.dataset.label = button.textContent);
      button.textContent = label;
      status.textContent = label;
      clearTimeout(button.timer);
      button.timer = setTimeout(() => { button.textContent = original; }, 2000);
    }
    section.querySelector('[data-action="copy"]').addEventListener("click", async (event) => {
      const button = event.currentTarget;
      if (blocked(button)) return;
      try {
        await navigator.clipboard.writeText(text);
        done(button, "Скопировано");
      } catch {
        const range = document.createRange();
        range.selectNodeContents(pre);
        getSelection().removeAllRanges();
        getSelection().addRange(range);
        done(button, "Выделено — нажмите Ctrl+C");
      }
    });
    section.querySelector('[data-action="download"]').addEventListener("click", (event) => {
      if (blocked(event.currentTarget)) return;
      const url = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
      const link = Object.assign(document.createElement("a"), { href: url, download: section.dataset.file });
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      done(event.currentTarget, "Скачано");
    });
  });
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
