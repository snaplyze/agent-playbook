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

ICON = {
    "check": '<svg class="icon icon-check" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.6l2.8 2.8 6.2-6.6"/></svg>',
    "chevron": '<svg class="icon icon-chevron" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6.5l4 3.5 4-3.5"/></svg>',
    "copy": '<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.6"/>'
            '<path d="M10.5 5.5V3.6A1.1 1.1 0 0 0 9.4 2.5H3.6A1.1 1.1 0 0 0 2.5 3.6v5.8a1.1 1.1 0 0 0 1.1 1.1h1.9"/></svg>',
    "download": '<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2.5v8M4.6 7.3L8 10.7l3.4-3.4M3 13.5h10"/></svg>',
    "reset": '<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.2 8a4.8 4.8 0 1 0 1.4-3.4M3 2.6v2.8h2.8"/></svg>',
    "auto": '<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="5.5"/><path d="M8 2.5a5.5 5.5 0 0 1 0 11z" fill="currentColor" stroke="none"/></svg>',
    "sun": '<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="2.8"/>'
           '<path d="M8 1.8v1.7M8 12.5v1.7M1.8 8h1.7M12.5 8h1.7M3.6 3.6l1.2 1.2M11.2 11.2l1.2 1.2M3.6 12.4l1.2-1.2M11.2 4.8l1.2-1.2"/></svg>',
    "moon": '<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M13.2 9.6A5.6 5.6 0 0 1 6.4 2.8a5.6 5.6 0 1 0 6.8 6.8z"/></svg>',
}


def inline(text):
    text = html.escape(text, quote=False)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", text)


def param_field(scenario, index, param):
    fid = f"{scenario['id']}-{index}"
    esc = html.escape
    help_html = f'<p class="help" id="{fid}-help">{inline(param["help"])}</p>'
    if "choices" in param:
        checked = " checked" if param["default"] == "all" else ""
        chips = "".join(f'<label class="chip"><input type="checkbox" value="{esc(c)}"{checked}>'
                        f'<span class="chip-box" aria-hidden="true">{ICON["check"]}</span><span>{esc(c)}</span></label>'
                        for c in param["choices"])
        return (f'<div class="field wide" data-index="{index}" role="group" aria-labelledby="{fid}-label" '
                f'aria-describedby="{fid}-help"><span class="label" id="{fid}-label">{esc(param["key"])}</span>'
                f'<div class="chips">{chips}</div>{help_html}</div>')
    options = "".join(
        f'<li class="option" role="option" id="{fid}-o{i}" data-value="{i}" aria-selected="{"true" if i == 0 else "false"}">'
        f'<span>{esc(o["label"])}</span>{ICON["check"]}</li>' for i, o in enumerate(param["option"]))
    extra = ""
    if any("input" in o for o in param["option"]):
        extra += (f'<input type="text" class="extra-input" hidden autocomplete="off" spellcheck="false" '
                  f'aria-label="{esc(param["key"])}: значение" aria-describedby="{fid}-note">'
                  f'<p class="note" id="{fid}-note" hidden>Нужно значение: без него промпт не скопируется.</p>')
    if any("choice" in o for o in param["option"]):
        extra += f'<div class="segmented extra-choice" role="radiogroup" hidden aria-label="{esc(param["key"])}: область"></div>'
    extra = f'<div class="extra" data-open="false"><div>{extra}</div></div>' if extra else ""
    # Комбобокс по образцу WAI-ARIA APG «Select-Only Combobox»: кнопка-триггер + список в popover.
    return (f'<div class="field" data-index="{index}"><label class="label" id="{fid}-label" for="{fid}">{esc(param["key"])}</label>'
            f'<div class="select"><button type="button" class="select-trigger" id="{fid}" role="combobox" '
            f'aria-haspopup="listbox" aria-expanded="false" aria-controls="{fid}-list" aria-describedby="{fid}-help" '
            f'popovertarget="{fid}-pop" data-value="0"><span class="select-value">{esc(param["option"][0]["label"])}</span>'
            f'{ICON["chevron"]}</button><div class="listbox-pop" id="{fid}-pop" popover>'
            f'<p class="listbox-title" aria-hidden="true">{esc(param["key"])}</p>'
            f'<ul class="listbox" id="{fid}-list" role="listbox" aria-labelledby="{fid}-label">{options}</ul></div></div>'
            f'{extra}{help_html}</div>')


def scenario_section(scenario, built):
    fields = "".join(param_field(scenario, i, p) for i, p in enumerate(scenario["params"]))
    sid = scenario["id"]
    params = (f'<div class="params" role="group" aria-labelledby="{sid}-params"><div class="params-head">'
              f'<h3 id="{sid}-params">Параметры</h3><button type="button" class="btn-text" data-action="reset" disabled>'
              f'{ICON["reset"]}Сбросить</button></div><div class="fields">{fields}</div></div>') if fields else ""
    return f"""
<section class="scenario" id="{sid}" aria-labelledby="{sid}-title" data-file="{scenario['file']}">
  <h2 id="{sid}-title"><span class="num">{scenario['num']}</span>{html.escape(scenario['name'])}</h2>
  <dl class="facts">
    <div><dt>Когда</dt><dd>{inline(scenario['when'])}</dd></div>
    <div><dt>Результат</dt><dd>{inline(scenario['result'])}</dd></div>
    <div><dt>Чего не делает</dt><dd>{inline(scenario['never'])}</dd></div>
  </dl>
  {params}
  <div class="bar-sentinel" aria-hidden="true"></div>
  <div class="bar">
    <button type="button" class="btn btn-primary" data-action="copy"><span class="btn-icons">{ICON["copy"]}{ICON["check"]}</span><span class="btn-label">Скопировать промпт</span></button>
    <button type="button" class="btn btn-secondary" data-action="download"><span class="btn-icons">{ICON["download"]}{ICON["check"]}</span><span class="btn-label">Скачать .md</span></button>
    <span class="meta"></span>
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
            .replace("@ICON_AUTO@", ICON["auto"]).replace("@ICON_SUN@", ICON["sun"]).replace("@ICON_MOON@", ICON["moon"])
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
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>agent-playbook</title>
<meta name="description" content="Самодостаточные промпты для AI-агентов, которые работают с кодом: выберите сценарий и параметры, скопируйте готовый текст.">
<meta property="og:title" content="agent-playbook">
<meta property="og:description" content="Самодостаточные промпты для AI-агентов, которые работают с кодом.">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" media="(prefers-color-scheme: light)" content="#EDF0F4">
<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#0E1117">
<script>try{var t=localStorage.getItem("theme");if(t==="light"||t==="dark")document.documentElement.dataset.theme=t}catch(e){}</script>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect x='3' y='3' width='26' height='26' rx='5' fill='none' stroke='%235B3FA8' stroke-width='3'/%3E%3Crect x='9' y='9' width='14' height='14' rx='2' fill='%235B3FA8'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Golos+Text:wght@400;500;600&family=JetBrains+Mono:wght@400;600&family=PT+Serif:wght@700&display=swap">
<style>
:root {
  --paper: #EDF0F4; --sheet: #FFFFFF; --well: #F5F6F9;
  --ink: #161B26; --ink-soft: #5B6475; --ink-faint: #8A93A2; --on-ink: #FFFFFF;
  --rule: #D5DAE2; --rule-strong: #B7BFCB;
  --accent: #5637A8; --accent-ink: #43298A; --accent-wash: #ECE7FA; --accent-wash-2: #DCD3F5; --on-accent: #FFFFFF;
  --code-ink: #2A6A56; --ok: #1F7F56; --on-ok: #FFFFFF; --toast-ok: #8FE3BE;
  --warn: #9E4A12; --on-warn: #FFFFFF; --warn-wash: #FBEFE4;
  --shadow-pop: 0 1px 2px rgb(22 27 38 / .08), 0 12px 32px -8px rgb(22 27 38 / .28);
  --shadow-bar: 0 -8px 24px -12px rgb(22 27 38 / .35);
  --serif: "PT Serif", Georgia, "Times New Roman", serif;
  --sans: "Golos Text", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --ease: cubic-bezier(.2, 0, 0, 1); --ease-out: cubic-bezier(.05, .7, .1, 1); --ease-in: cubic-bezier(.3, 0, .8, .15);
  --d1: 100ms; --d2: 200ms; --d3: 300ms;
  --r1: 7px; --r2: 10px; --r3: 14px; --gutter: 16px; --bar-h: 0px;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --paper: #0E1117; --sheet: #161A22; --well: #1C212B;
    --ink: #E7E9EE; --ink-soft: #A3ABB9; --ink-faint: #6E7786; --on-ink: #0E1117;
    --rule: #2A303C; --rule-strong: #3B4453;
    --accent: #B49BFF; --accent-ink: #CDBBFF; --accent-wash: #2A2342; --accent-wash-2: #3A3060; --on-accent: #0E1117;
    --code-ink: #8ED1B8; --ok: #2E9B6B; --on-ok: #0E1117; --toast-ok: #1F7F56;
    --warn: #E09A5E; --on-warn: #0E1117; --warn-wash: #3A2A1E;
    --shadow-pop: 0 1px 2px rgb(0 0 0 / .4), 0 12px 32px -8px rgb(0 0 0 / .6);
    --shadow-bar: 0 -8px 24px -12px rgb(0 0 0 / .7);
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --paper: #0E1117; --sheet: #161A22; --well: #1C212B;
  --ink: #E7E9EE; --ink-soft: #A3ABB9; --ink-faint: #6E7786; --on-ink: #0E1117;
  --rule: #2A303C; --rule-strong: #3B4453;
  --accent: #B49BFF; --accent-ink: #CDBBFF; --accent-wash: #2A2342; --accent-wash-2: #3A3060; --on-accent: #0E1117;
  --code-ink: #8ED1B8; --ok: #2E9B6B; --on-ok: #0E1117; --toast-ok: #1F7F56;
  --warn: #E09A5E; --on-warn: #0E1117; --warn-wash: #3A2A1E;
  --shadow-pop: 0 1px 2px rgb(0 0 0 / .4), 0 12px 32px -8px rgb(0 0 0 / .6);
  --shadow-bar: 0 -8px 24px -12px rgb(0 0 0 / .7);
  color-scheme: dark;
}

* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; scrollbar-gutter: stable; }
body { margin: 0; background: var(--paper); color: var(--ink); font: 400 16px/1.5 var(--sans); -webkit-font-smoothing: antialiased; }
a { color: inherit; text-decoration-color: var(--accent); text-underline-offset: 3px; transition: color var(--d2) var(--ease); }
a:hover { color: var(--accent); }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
button { font: inherit; color: inherit; -webkit-tap-highlight-color: transparent; }
code { font: 0.86em/1.4 var(--mono); background: var(--sheet); border: 1px solid var(--rule); border-radius: 4px; padding: 0 0.3em; -webkit-box-decoration-break: clone; box-decoration-break: clone; }
.icon { width: 1em; height: 1em; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; flex: none; }

.wrap { max-width: 1180px; margin: 0 auto; padding: 0 var(--gutter); }
.top { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 8px 16px; padding: 18px 0 0; font-size: 15px; color: var(--ink-soft); }
.brand { color: var(--ink); font-weight: 600; }
.top-right { display: flex; align-items: center; gap: 16px; }
.theme { display: inline-grid; grid-auto-flow: column; gap: 2px; padding: 3px; border: 1px solid var(--rule); border-radius: 999px; background: var(--sheet); }
.theme button { display: grid; place-items: center; width: 36px; height: 36px; border: 0; border-radius: 999px; background: transparent; color: var(--ink-faint); cursor: pointer; transition: background var(--d2) var(--ease), color var(--d2) var(--ease), transform var(--d1) var(--ease); }
.theme button:active { transform: scale(.9); }
.theme button[aria-pressed="true"] { background: var(--accent-wash); color: var(--accent-ink); }
@media (hover: hover) { .theme button:hover { color: var(--ink); } }

.intro { padding: clamp(28px, 5vw, 48px) 0 clamp(24px, 4vw, 36px); max-width: 46rem; }
.intro h1 { font: 700 clamp(1.85rem, 1.2rem + 2.6vw, 3rem)/1.1 var(--serif); letter-spacing: -0.015em; margin: 0 0 16px; text-wrap: balance; }
.intro p { font-size: clamp(1rem, .95rem + .4vw, 1.125rem); line-height: 1.55; color: var(--ink-soft); margin: 0; max-width: 42rem; text-wrap: pretty; }

.layout { display: grid; grid-template-columns: clamp(180px, 18vw, 224px) minmax(0, 1fr); gap: clamp(28px, 4vw, 56px); align-items: start; padding-bottom: 64px; }
.rail { position: sticky; top: 24px; }
.rail ul { position: relative; list-style: none; margin: 0; padding: 0; border-left: 1px solid var(--rule); }
.rail ul::before { content: ""; position: absolute; left: -2px; top: var(--ind-top, 0px); height: var(--ind-h, 44px); width: 3px; border-radius: 2px; background: var(--accent); transition: top var(--d3) var(--ease), height var(--d3) var(--ease); }
.rail a { display: flex; gap: 12px; align-items: baseline; min-height: 44px; padding: 10px 8px 10px 16px; text-decoration: none; color: var(--ink-soft); border-radius: 0 var(--r1) var(--r1) 0; transition: color var(--d2) var(--ease), background var(--d2) var(--ease); }
@media (hover: hover) { .rail a:hover { color: var(--ink); background: color-mix(in srgb, var(--sheet) 70%, transparent); } }
.rail a[aria-current="page"] { color: var(--ink); font-weight: 500; }
.num { font-variant-numeric: tabular-nums; color: var(--accent); font-weight: 500; }
.rail .order { margin: 20px 0 0; padding-left: 16px; font-size: 14px; line-height: 1.5; color: var(--ink-soft); }

main { view-transition-name: main; min-width: 0; }
.scenario { scroll-margin-top: 16px; }
.scenario + .scenario { margin-top: 72px; }
.js .scenario + .scenario { margin-top: 0; }
.scenario h2 { display: flex; align-items: baseline; gap: 12px; margin: 0 0 18px; font: 700 clamp(1.6rem, 1.25rem + 1.5vw, 2.25rem)/1.15 var(--serif); letter-spacing: -0.01em; }
.scenario h2 .num { font: 500 1rem/1 var(--sans); }
.facts { display: grid; grid-template-columns: max-content minmax(0, 1fr); margin: 0 0 24px; max-width: 64rem; border-top: 1px solid var(--rule); }
.facts > div { display: grid; grid-template-columns: subgrid; grid-column: 1 / -1; gap: 4px 28px; padding: 10px 0; border-bottom: 1px solid var(--rule); }
.facts dt { color: var(--ink-soft); font-size: 14px; padding-top: 2px; }
.facts dd { margin: 0; line-height: 1.5; }

.params { container-type: inline-size; background: var(--sheet); border: 1px solid var(--rule); border-radius: var(--r3); padding: 16px 20px 20px; margin: 0 0 16px; }
html:not(.js) .params, html:not(.js) .theme { display: none; }
.params-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; min-height: 28px; margin: 0 0 14px; }
.params-head h3 { margin: 0; font: 600 15px/1.2 var(--sans); color: var(--ink-soft); }
.btn-text { display: inline-flex; align-items: center; gap: 6px; min-height: 36px; margin: -8px -8px; padding: 4px 8px; border: 0; border-radius: var(--r1); background: none; font: 500 14px/1 var(--sans); color: var(--accent); cursor: pointer; transition: opacity var(--d2) var(--ease), background var(--d2) var(--ease), transform var(--d1) var(--ease); }
.btn-text:disabled { opacity: 0; pointer-events: none; }
.btn-text:active { transform: scale(.96); }
@media (hover: hover) { .btn-text:hover { background: var(--accent-wash); } }
.fields { display: grid; gap: 18px 24px; grid-template-columns: minmax(0, 1fr); }
@container (min-width: 560px) { .fields { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@container (min-width: 760px) { .fields { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
.field { display: grid; gap: 8px; align-content: start; min-width: 0; }
.field.wide { grid-column: 1 / -1; }
.label { font-weight: 500; }
.field.is-pulse > .label { animation: label-pulse .9s var(--ease); }
@keyframes label-pulse { 0%, 40% { color: var(--accent); } }
.help { margin: 0; font-size: 14px; line-height: 1.45; color: var(--ink-soft); }

.select { position: relative; }
.select-trigger { display: flex; align-items: center; justify-content: space-between; gap: 10px; width: 100%; min-height: 46px; padding: 10px 12px 10px 14px; text-align: left; font: 400 16px/1.3 var(--sans); color: var(--ink); background: var(--well); border: 1px solid var(--rule); border-radius: var(--r2); cursor: pointer; transition: border-color var(--d2) var(--ease), background var(--d2) var(--ease), box-shadow var(--d2) var(--ease), transform var(--d1) var(--ease); }
.select-value { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.icon-chevron { color: var(--ink-faint); transition: rotate var(--d2) var(--ease), color var(--d2) var(--ease); }
@media (hover: hover) { .select-trigger:hover { border-color: var(--rule-strong); background: var(--sheet); } }
.select-trigger:active { transform: scale(.99); }
.select-trigger:focus-visible { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-wash-2); }
.select-trigger[aria-expanded="true"] { border-color: var(--accent); background: var(--sheet); box-shadow: 0 0 0 3px var(--accent-wash); }
.select-trigger[aria-expanded="true"] .icon-chevron { rotate: 180deg; color: var(--accent); }

.listbox-pop { display: none; position: fixed; z-index: 30; inset: auto; margin: 0; padding: 6px; width: 260px; max-height: 320px; overflow: auto; overscroll-behavior: contain; border: 1px solid var(--rule); border-radius: var(--r2); background: var(--sheet); color: var(--ink); box-shadow: var(--shadow-pop); --from: -6px; opacity: 0; translate: 0 var(--from); transition: opacity var(--d2) var(--ease), translate var(--d2) var(--ease), display var(--d2) allow-discrete, overlay var(--d2) allow-discrete; }
.listbox-pop[data-side="top"] { --from: 6px; }
.listbox-pop:is(:popover-open, .is-open) { display: block; opacity: 1; translate: 0 0; }
@starting-style { .listbox-pop:is(:popover-open, .is-open) { opacity: 0; translate: 0 var(--from); } }
.listbox-pop::backdrop { background: transparent; transition: background var(--d2) var(--ease), display var(--d2) allow-discrete, overlay var(--d2) allow-discrete; }
.listbox-title { display: none; margin: 0; padding: 4px 12px 10px; font: 600 14px/1.2 var(--sans); color: var(--ink-soft); }
.listbox { list-style: none; margin: 0; padding: 0; }
.option { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 42px; padding: 9px 12px; border-radius: var(--r1); line-height: 1.3; cursor: pointer; transition: background var(--d1) var(--ease), color var(--d1) var(--ease); }
.option.is-active { background: var(--accent-wash); color: var(--accent-ink); }
.option[aria-selected="true"] { font-weight: 500; }
.option .icon-check { color: var(--accent); opacity: 0; scale: .6; transition: opacity var(--d1) var(--ease), scale var(--d1) var(--ease); }
.option[aria-selected="true"] .icon-check { opacity: 1; scale: 1; }
.listbox-pop.sheet { inset: auto 0 0 0; width: auto; max-height: min(70dvh, 520px); padding: 10px 10px calc(10px + env(safe-area-inset-bottom)); border-bottom: 0; border-radius: var(--r3) var(--r3) 0 0; --from: 100%; }
.listbox-pop.sheet::before { content: ""; display: block; width: 36px; height: 4px; margin: 0 auto 8px; border-radius: 2px; background: var(--rule-strong); }
.listbox-pop.sheet .listbox-title { display: block; }
.listbox-pop.sheet .option { min-height: 48px; font-size: 16px; }
.listbox-pop.sheet:is(:popover-open, .is-open)::backdrop { background: rgb(10 12 18 / .45); }
@starting-style { .listbox-pop.sheet:is(:popover-open, .is-open)::backdrop { background: transparent; } }

.extra { display: grid; grid-template-rows: 0fr; margin-top: -8px; transition: grid-template-rows var(--d3) var(--ease), margin-top var(--d3) var(--ease); }
.extra[data-open="true"] { grid-template-rows: 1fr; margin-top: 0; }
.extra > div { display: grid; grid-template-columns: minmax(0, 1fr); gap: 8px; min-height: 0; overflow: hidden; padding: 4px; margin: -4px; }
.extra > div > [hidden] { display: none; }
.extra-input { width: 100%; min-width: 0; min-height: 44px; padding: 10px 12px; font: 400 15px/1.4 var(--mono); color: var(--ink); background: var(--well); border: 1px solid var(--rule); border-radius: var(--r2); transition: border-color var(--d2) var(--ease), box-shadow var(--d2) var(--ease), background var(--d2) var(--ease); }
.extra-input::placeholder { color: var(--ink-soft); }
.extra-input:focus { outline: none; border-color: var(--accent); background: var(--sheet); box-shadow: 0 0 0 3px var(--accent-wash); }
.extra-input[aria-invalid="true"] { border-color: var(--warn); }
.extra-input[aria-invalid="true"]:focus { box-shadow: 0 0 0 3px var(--warn-wash); }
.extra-input.is-shake { animation: shake .4s var(--ease); }
@keyframes shake { 20%, 60% { translate: -4px 0; } 40%, 80% { translate: 4px 0; } }
.note { margin: 0; font-size: 13px; line-height: 1.4; color: var(--warn); }
.segmented { display: inline-grid; grid-auto-flow: column; grid-auto-columns: 1fr; gap: 2px; justify-self: start; padding: 3px; border: 1px solid var(--rule); border-radius: var(--r2); background: var(--well); }
.seg { position: relative; display: grid; place-items: center; min-height: 36px; padding: 0 14px; border-radius: calc(var(--r2) - 3px); color: var(--ink-soft); font-size: 15px; white-space: nowrap; cursor: pointer; transition: background var(--d2) var(--ease), color var(--d2) var(--ease), box-shadow var(--d2) var(--ease); }
.seg input { position: absolute; inset: 0; width: 100%; height: 100%; margin: 0; opacity: 0; font: inherit; cursor: pointer; }
.seg:has(input:checked) { background: var(--sheet); color: var(--ink); box-shadow: 0 1px 2px rgb(0 0 0 / .14); }
.seg:has(input:focus-visible) { outline: 2px solid var(--accent); outline-offset: -1px; }

.chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip { position: relative; display: inline-flex; align-items: center; gap: 9px; min-height: 40px; padding: 6px 14px 6px 10px; border: 1px solid var(--rule); border-radius: 999px; background: var(--well); line-height: 1.3; cursor: pointer; -webkit-user-select: none; user-select: none; transition: background var(--d2) var(--ease), border-color var(--d2) var(--ease), color var(--d2) var(--ease), transform var(--d1) var(--ease); }
.chip:active { transform: scale(.97); }
@media (hover: hover) { .chip:hover { border-color: var(--rule-strong); } }
.chip input { position: absolute; inset: 0; width: 100%; height: 100%; margin: 0; opacity: 0; font: inherit; cursor: pointer; }
.chip-box { display: grid; place-items: center; width: 18px; height: 18px; border: 1.5px solid var(--ink-faint); border-radius: 5px; background: var(--sheet); transition: background var(--d2) var(--ease), border-color var(--d2) var(--ease); }
.chip-box .icon { width: 12px; height: 12px; stroke: var(--on-accent); stroke-width: 2.4; stroke-dasharray: 16; stroke-dashoffset: 16; transition: stroke-dashoffset var(--d2) var(--ease); }
.chip:has(input:checked) { background: var(--accent-wash); border-color: var(--accent-wash-2); color: var(--accent-ink); }
.chip:has(input:checked) .chip-box { background: var(--accent); border-color: var(--accent); }
.chip:has(input:checked) .chip-box .icon { stroke-dashoffset: 0; }
.chip:has(input:focus-visible) { outline: 2px solid var(--accent); outline-offset: 2px; }

.bar-sentinel { height: 1px; margin-top: -1px; }
.bar { display: none; position: sticky; top: 0; z-index: 5; flex-wrap: wrap; gap: 10px; align-items: center; padding: 10px 0; margin: 0 0 12px; background: var(--paper); background: color-mix(in srgb, var(--paper) 88%, transparent); -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px); border-bottom: 1px solid transparent; transition: border-color var(--d2) var(--ease); }
.js .bar { display: flex; }
.bar.is-stuck { border-bottom-color: var(--rule); }
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 10px; min-height: 46px; padding: 0 18px; border: 1px solid transparent; border-radius: var(--r2); font: 500 15px/1.2 var(--sans); cursor: pointer; transition: background var(--d2) var(--ease), color var(--d2) var(--ease), border-color var(--d2) var(--ease), transform var(--d1) var(--ease); }
.btn:active { transform: translateY(1px) scale(.98); }
.btn-primary { background: var(--ink); color: var(--on-ink); border-color: var(--ink); min-width: 13.5em; }
.btn-secondary { background: transparent; color: var(--ink); border-color: var(--rule-strong); }
@media (hover: hover) {
  .btn-primary:hover { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
  .btn-secondary:hover { border-color: var(--accent); color: var(--accent); }
}
.btn-icons { position: relative; width: 18px; height: 18px; flex: none; }
.btn-icons .icon { position: absolute; inset: 0; width: 100%; height: 100%; transition: opacity var(--d2) var(--ease), scale var(--d2) var(--ease-out), rotate var(--d2) var(--ease-out); }
.btn-icons .icon-check { opacity: 0; scale: .4; rotate: -20deg; }
.btn.is-done .btn-icons .icon { opacity: 0; scale: .4; }
.btn.is-done .btn-icons .icon-check { opacity: 1; scale: 1; rotate: 0deg; }
.btn.is-done, .btn.is-done:hover { background: var(--ok); border-color: var(--ok); color: var(--on-ok); }
.meta { margin-left: auto; font-size: 14px; font-variant-numeric: tabular-nums; color: var(--ink-soft); }

.prompt { margin: 0; padding: clamp(16px, 3vw, 28px) clamp(16px, 3vw, 32px); background: var(--sheet); border: 1px solid var(--rule); border-radius: var(--r3); font: 400 14px/1.7 var(--mono); white-space: pre-wrap; overflow-wrap: anywhere; tab-size: 2; }
.prompt .h { font-weight: 600; color: var(--ink); }
.prompt .m { color: var(--accent); }
.prompt .c { color: var(--code-ink); }
.prompt .b { font-weight: 600; }
.prompt .p { background: var(--accent-wash); box-shadow: 0 0 0 2px var(--accent-wash); border-radius: 2px; -webkit-box-decoration-break: clone; box-decoration-break: clone; }
.prompt .p.changed { animation: changed 1.2s var(--ease); }
@keyframes changed { 0% { background: var(--accent); color: var(--on-accent); box-shadow: 0 0 0 2px var(--accent); } 100% { background: var(--accent-wash); } }

.toasts { position: fixed; z-index: 40; right: 16px; bottom: 16px; display: grid; gap: 8px; width: min(380px, calc(100vw - 32px)); pointer-events: none; }
.toast { pointer-events: auto; position: relative; overflow: hidden; display: grid; grid-template-columns: 20px minmax(0, 1fr); gap: 10px; align-items: start; padding: 12px 14px 13px; border-radius: var(--r2); background: var(--ink); color: var(--on-ink); box-shadow: var(--shadow-pop); font-size: 14px; line-height: 1.4; cursor: pointer; animation: toast-in var(--d3) var(--ease-out) both; }
.toast .icon { width: 20px; height: 20px; }
.toast.ok .icon { color: var(--toast-ok); }
.toast.warn { background: var(--warn); color: var(--on-warn); }
.toast::after { content: ""; position: absolute; left: 0; bottom: 0; width: 100%; height: 2px; background: currentColor; opacity: .35; transform-origin: left; animation: toast-life var(--life, 4s) linear forwards; }
.toast:hover::after { animation-play-state: paused; }
.toast.is-leaving { animation: toast-out var(--d2) var(--ease-in) both; }
@keyframes toast-in { from { opacity: 0; translate: 0 12px; scale: .98; } }
@keyframes toast-out { to { opacity: 0; translate: 0 8px; } }
@keyframes toast-life { to { transform: scaleX(0); } }

::view-transition-old(main) { animation: var(--d2) var(--ease-in) both vt-out; }
::view-transition-new(main) { animation: var(--d3) var(--ease-out) both vt-in; }
::view-transition-old(root), ::view-transition-new(root) { animation: none; mix-blend-mode: normal; }
@keyframes vt-out { to { opacity: 0; translate: 0 -8px; } }
@keyframes vt-in { from { opacity: 0; translate: 0 10px; } }

footer { border-top: 1px solid var(--rule); padding: 24px 0 40px; font-size: 14px; color: var(--ink-soft); }
footer p { margin: 0; max-width: 46rem; }

@media (max-width: 960px) {
  .layout { grid-template-columns: minmax(0, 1fr); gap: 24px; }
  .rail { position: static; }
  .rail ul { display: flex; gap: 8px; overflow-x: auto; border-left: 0; padding: 4px; margin: -4px; scroll-snap-type: x proximity; scrollbar-width: none; }
  .rail ul::-webkit-scrollbar { display: none; }
  .rail ul::before { display: none; }
  .rail li { flex: 0 0 auto; scroll-snap-align: start; }
  .rail a { margin: 0; min-height: 40px; padding: 8px 14px 8px 12px; gap: 8px; align-items: center; border: 1px solid var(--rule); border-radius: 999px; background: var(--sheet); font-size: 15px; white-space: nowrap; transition: background var(--d2) var(--ease), border-color var(--d2) var(--ease), color var(--d2) var(--ease); }
  .rail a[aria-current="page"] { border-color: var(--accent); background: var(--accent-wash); color: var(--accent-ink); font-weight: 500; }
  .rail .order { padding-left: 0; margin-top: 12px; }
}
@media (max-width: 640px) {
  :root { --bar-h: 96px; }
  .top { padding-top: 14px; }
  .top-right { gap: 12px; }
  .facts { grid-template-columns: minmax(0, 1fr); }
  .facts > div { grid-template-columns: minmax(0, 1fr); gap: 2px; }
  .params { padding: 14px 14px 16px; border-radius: var(--r2); }
  .scenario { display: flex; flex-direction: column; }
  .scenario[hidden] { display: none; }
  .bar-sentinel { display: none; }
  .js .bar { order: 10; position: sticky; top: auto; bottom: 0; margin: 12px calc(-1 * var(--gutter)) 0; padding: 10px var(--gutter) calc(10px + env(safe-area-inset-bottom)); border-top: 1px solid var(--rule); border-bottom: 0; box-shadow: var(--shadow-bar); }
  .bar .btn { flex: 1 1 auto; min-width: 0; padding: 0 12px; }
  .meta { order: -1; width: 100%; margin: 0; font-size: 13px; text-align: center; }
  .toasts { left: 16px; right: 16px; width: auto; bottom: calc(var(--bar-h) + env(safe-area-inset-bottom) + 12px); }
  .prompt { font-size: 13px; line-height: 1.65; }
}
@media (prefers-reduced-motion: reduce) {
  .listbox-pop, .listbox-pop[data-side="top"], .listbox-pop.sheet { --from: 0px; }
  .btn:active, .btn-text:active, .chip:active, .select-trigger:active, .theme button:active { transform: none; }
  .extra-input.is-shake { animation: none; }
  .extra { transition: none; }
  .rail ul::before { transition: none; }
  .prompt .p.changed { animation-duration: .5s; }
  @keyframes toast-in { from { opacity: 0; } }
  @keyframes toast-out { to { opacity: 0; } }
  @keyframes vt-out { to { opacity: 0; } }
  @keyframes vt-in { from { opacity: 0; } }
}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <strong class="brand">agent-playbook</strong>
    <div class="top-right">
      <a href="https://github.com/@REPO@">Репозиторий на GitHub</a>
      <div class="theme" role="group" aria-label="Тема оформления">
        <button type="button" data-theme-set="" aria-pressed="true" title="Тема как в системе" aria-label="Тема как в системе">@ICON_AUTO@</button>
        <button type="button" data-theme-set="light" aria-pressed="false" title="Светлая тема" aria-label="Светлая тема">@ICON_SUN@</button>
        <button type="button" data-theme-set="dark" aria-pressed="false" title="Тёмная тема" aria-label="Тёмная тема">@ICON_MOON@</button>
      </div>
    </div>
  </div>
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
<div class="toasts" role="status" aria-live="polite" aria-atomic="false"></div>
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
  const root = document.documentElement;
  root.classList.add("js");
  const { scenarios: data, missing } = JSON.parse(document.getElementById("data").textContent);
  const { clean, compose } = playbook;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const narrow = matchMedia("(max-width: 640px)");
  const coarse = matchMedia("(pointer: coarse)");
  const stacked = matchMedia("(max-width: 960px)");
  const popoverApi = "showPopover" in HTMLElement.prototype;
  const esc = (value) => value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const plural = (n) => n % 10 === 1 && n % 100 !== 11 ? "символ" : [2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100) ? "символа" : "символов";
  const count = (n) => n.toLocaleString("ru-RU") + " " + plural(n);
  const replay = (el, cls) => { el.classList.remove(cls); void el.offsetWidth; el.classList.add(cls); };

  // --- Тема: системная, светлая или тёмная; выбор хранится в localStorage.
  const themeButtons = [...document.querySelectorAll("[data-theme-set]")];
  function applyTheme(value) {
    if (value) root.dataset.theme = value; else delete root.dataset.theme;
    const dark = value === "dark" || (!value && matchMedia("(prefers-color-scheme: dark)").matches);
    document.querySelectorAll('meta[name="theme-color"]').forEach((m) => m.setAttribute("content", dark ? "#0E1117" : "#EDF0F4"));
    themeButtons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.themeSet === (value || ""))));
  }
  themeButtons.forEach((b) => b.addEventListener("click", () => {
    applyTheme(b.dataset.themeSet);
    try { b.dataset.themeSet ? localStorage.setItem("theme", b.dataset.themeSet) : localStorage.removeItem("theme"); } catch {}
  }));
  applyTheme(root.dataset.theme || "");

  // --- Уведомления: контейнер role=status живёт в DOM постоянно, фокус не переводится,
  // исчезают сами (факт уже в буфере обмена или в файле), пауза при наведении, клик закрывает.
  const toasts = document.querySelector(".toasts");
  const TOAST_ICON = {
    ok: '<svg class="icon" viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="7.5"/><path d="M6.5 10.3l2.4 2.4L13.6 8"/></svg>',
    warn: '<svg class="icon" viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="7.5"/><path d="M10 6.5v4.2M10 13.6v.2"/></svg>'
  };
  function toast(kind, text, life = 4200) {
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.style.setProperty("--life", life + "ms");
    el.innerHTML = TOAST_ICON[kind] + "<span></span>";
    el.lastElementChild.textContent = text;
    const leave = () => {
      if (el.classList.contains("is-leaving")) return;
      el.classList.add("is-leaving");
      el.addEventListener("animationend", (e) => { if (e.animationName === "toast-out") el.remove(); });
      setTimeout(() => el.remove(), 600);
    };
    el.addEventListener("animationend", (e) => { if (e.animationName === "toast-life") leave(); });
    el.addEventListener("click", leave);
    while (toasts.children.length >= 3) toasts.firstElementChild.remove();
    toasts.append(el);
  }

  // --- Кастомный select: WAI-ARIA APG «Select-Only Combobox» (фокус остаётся на кнопке,
  // aria-activedescendant указывает на подсвеченный пункт). Список — popover в верхнем слое;
  // на узких экранах и при сенсорном вводе — нижняя шторка.
  function makeSelect(field, param, onChange) {
    const trigger = field.querySelector(".select-trigger");
    const pop = field.querySelector(".listbox-pop");
    const options = [...pop.querySelectorAll('[role="option"]')];
    const valueEl = trigger.querySelector(".select-value");
    const labels = param.option.map((o) => o.label.toLowerCase());
    let current = Number(trigger.dataset.value), active = current, typed = "", typeTimer = 0;
    const isOpen = () => popoverApi ? pop.matches(":popover-open") : pop.classList.contains("is-open");

    function place() {
      const sheet = narrow.matches || coarse.matches;
      pop.classList.toggle("sheet", sheet);
      pop.style.cssText = "";
      if (sheet) return;
      const r = trigger.getBoundingClientRect();
      const need = Math.min(pop.scrollHeight || options.length * 42 + 14, 320);
      const below = innerHeight - r.bottom - 12, above = r.top - 12;
      const up = below < need && above > below;
      pop.dataset.side = up ? "top" : "bottom";
      pop.style.left = r.left + "px";
      pop.style.width = r.width + "px";
      pop.style.maxHeight = Math.max(120, Math.min(320, up ? above : below)) + "px";
      if (up) pop.style.bottom = (innerHeight - r.top + 6) + "px"; else pop.style.top = (r.bottom + 6) + "px";
    }
    function setActive(i, scroll) {
      active = Math.max(0, Math.min(options.length - 1, i));
      options.forEach((o, k) => o.classList.toggle("is-active", k === active));
      trigger.setAttribute("aria-activedescendant", options[active].id);
      if (scroll) options[active].scrollIntoView({ block: "nearest" });
    }
    const onScroll = (e) => { if (!(e.target instanceof Node && pop.contains(e.target))) place(); };
    function opened(on) {
      trigger.setAttribute("aria-expanded", String(on));
      if (on) {
        place();
        setActive(active, true);
        addEventListener("scroll", onScroll, { capture: true, passive: true });
        addEventListener("resize", place);
      } else {
        trigger.removeAttribute("aria-activedescendant");
        removeEventListener("scroll", onScroll, { capture: true });
        removeEventListener("resize", place);
      }
    }
    const outside = (e) => { if (!pop.contains(e.target) && !trigger.contains(e.target)) closeList(); };
    function openList(focusIndex) {
      if (isOpen()) { if (focusIndex !== undefined) setActive(focusIndex, true); return; }
      active = focusIndex === undefined ? current : focusIndex;
      if (popoverApi) pop.showPopover();
      else { pop.classList.add("is-open"); opened(true); document.addEventListener("pointerdown", outside, true); }
    }
    function closeList() {
      if (!isOpen()) return;
      if (popoverApi) pop.hidePopover();
      else { pop.classList.remove("is-open"); opened(false); document.removeEventListener("pointerdown", outside, true); }
    }
    function set(i) {
      if (i === current) return false;
      current = i;
      trigger.dataset.value = String(i);
      valueEl.textContent = param.option[i].label;
      options.forEach((o, k) => o.setAttribute("aria-selected", String(k === i)));
      return true;
    }
    function commit(i) { const changed = set(i); closeList(); if (changed) onChange(); }
    if (popoverApi) {
      pop.addEventListener("beforetoggle", (e) => { if (e.newState === "open") place(); });
      pop.addEventListener("toggle", (e) => opened(e.newState === "open"));
    } else {
      trigger.addEventListener("click", () => isOpen() ? closeList() : openList());
    }
    trigger.addEventListener("keydown", (e) => {
      const k = e.key, open = isOpen();
      switch (k) {
        case "ArrowDown": e.preventDefault(); return open ? setActive(active + 1, true) : openList();
        case "ArrowUp":
          e.preventDefault();
          if (!open) return openList(0);
          return e.altKey ? commit(active) : setActive(active - 1, true);
        case "Enter": case " ": e.preventDefault(); return open ? commit(active) : openList();
        case "Tab": if (open) { commit(active); if (document.activeElement !== trigger) e.preventDefault(); } return;
        case "Escape": if (open) { e.preventDefault(); closeList(); } return;
        case "Home": e.preventDefault(); return open ? setActive(0, true) : openList(0);
        case "End": e.preventDefault(); return open ? setActive(options.length - 1, true) : openList(options.length - 1);
        case "PageUp": if (open) { e.preventDefault(); setActive(active - 10, true); } return;
        case "PageDown": if (open) { e.preventDefault(); setActive(active + 10, true); } return;
      }
      if (k.length !== 1 || e.ctrlKey || e.metaKey || e.altKey) return;
      e.preventDefault();
      clearTimeout(typeTimer);
      typed += k.toLowerCase();
      typeTimer = setTimeout(() => { typed = ""; }, 500);
      const same = typed.length > 1 && [...typed].every((c) => c === typed[0]);
      const needle = same ? typed[0] : typed;
      const start = same || typed.length === 1 ? active + 1 : active;
      for (let n = 0; n < labels.length; n++) {
        const i = (start + n) % labels.length;
        if (labels[i].startsWith(needle)) { open ? setActive(i, true) : openList(i); break; }
      }
    });
    trigger.addEventListener("keyup", (e) => { if (e.key === " ") e.preventDefault(); });
    options.forEach((o, i) => {
      o.addEventListener("pointermove", () => { if (i !== active) setActive(i); });
      o.addEventListener("click", () => { trigger.focus({ preventScroll: true }); commit(i); });
    });
    return { set, value: () => current };
  }

  // --- Переключение сценариев: один виден, остальные скрыты; переход через View Transitions, если есть.
  const sections = [...document.querySelectorAll(".scenario")];
  const links = [...document.querySelectorAll(".rail a[data-id]")];
  const rail = document.querySelector(".rail ul");
  function moveIndicator() {
    const link = rail.querySelector('a[aria-current="page"]');
    if (!link) return;
    rail.style.setProperty("--ind-top", link.offsetTop + "px");
    rail.style.setProperty("--ind-h", link.offsetHeight + "px");
    if (stacked.matches) rail.scrollTo({ left: link.offsetLeft - (rail.clientWidth - link.offsetWidth) / 2, behavior: reduced.matches ? "auto" : "smooth" });
  }
  function show(id, scroll) {
    sections.forEach((section) => { section.hidden = section.id !== id; });
    links.forEach((link) => link.dataset.id === id ? link.setAttribute("aria-current", "page") : link.removeAttribute("aria-current"));
    moveIndicator();
    const section = document.getElementById(id);
    if (scroll && section.getBoundingClientRect().top < 0) section.scrollIntoView({ block: "start" });
  }
  function go(id, scroll) {
    if (!document.getElementById(id).hidden) return show(id, scroll);
    const run = () => show(id, scroll);
    if (document.startViewTransition && !reduced.matches) document.startViewTransition(run); else run();
  }
  const current = () => sections.some((s) => s.id === location.hash.slice(1)) ? location.hash.slice(1) : sections[0].id;
  links.forEach((link) => link.addEventListener("click", (event) => {
    event.preventDefault();
    history.replaceState(null, "", "#" + link.dataset.id);
    go(link.dataset.id, true);
  }));
  addEventListener("hashchange", () => go(current(), true));
  show(current(), false);
  // Браузер прокрутил к якорю до того, как скрипт спрятал остальные промпты: выравниваем заново.
  if (location.hash) document.getElementById(current()).scrollIntoView({ block: "start" });
  addEventListener("resize", moveIndicator);
  if (document.fonts) document.fonts.ready.then(moveIndicator);

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

  sections.forEach((section) => {
    const model = data.find((item) => item.id === section.id);
    const pre = section.querySelector(".prompt");
    const meta = section.querySelector(".meta");
    const bar = section.querySelector(".bar");
    const resetButton = section.querySelector('[data-action="reset"]');
    const fields = [...section.querySelectorAll(".field[data-index]")];
    const selects = new Map();
    let text = "", previous = null;

    if ("IntersectionObserver" in window) {
      new IntersectionObserver(([entry]) => bar.classList.toggle("is-stuck", !entry.isIntersecting && entry.boundingClientRect.top < 0))
        .observe(section.querySelector(".bar-sentinel"));
    }
    fields.forEach((field) => {
      const param = model.params[field.dataset.index];
      if (param.choices) return;
      selects.set(field, makeSelect(field, param, () => {
        render();
        const input = field.querySelector(".extra-input:not([hidden])");
        if (input && !clean(input.value)) input.focus();
      }));
    });

    // Состояние элементов управления: видимость поля значения и второго выбора.
    function state(field) {
      const param = model.params[field.dataset.index];
      if (param.choices) return { picked: [...field.querySelectorAll("input:checked")].map((input) => input.value) };
      const index = selects.get(field).value();
      const option = param.option[index];
      const input = field.querySelector(".extra-input");
      const choice = field.querySelector(".extra-choice");
      const extra = field.querySelector(".extra");
      if (input) {
        const need = option.input !== undefined;
        const empty = need && !clean(input.value);
        input.hidden = !need;
        input.placeholder = option.input || "";
        input.setAttribute("aria-invalid", String(empty));
        field.querySelector(".note").hidden = !(empty && field.dataset.touched);
      }
      if (choice) {
        choice.hidden = !option.choice;
        if (option.choice && choice.dataset.for !== String(index)) {
          choice.innerHTML = option.choice.map((value, i) => '<label class="seg"><input type="radio" name="' + field.querySelector(".select-trigger").id +
            '-choice" value="' + esc(value) + '"' + (i ? "" : " checked") + "><span>" + esc(value) + "</span></label>").join("");
          choice.dataset.for = String(index);
        }
      }
      if (extra) extra.dataset.open = String(Boolean(input && !input.hidden || choice && !choice.hidden));
      return { index, input: input ? input.value : "", choice: choice && !choice.hidden ? choice.querySelector("input:checked").value : "" };
    }
    function isDefault() {
      return fields.every((field) => {
        const param = model.params[field.dataset.index];
        if (param.choices) {
          const picked = [...field.querySelectorAll("input:checked")].map((input) => input.value);
          const wanted = param.default === "all" ? param.choices : [];
          return picked.length === wanted.length && picked.every((value, i) => value === wanted[i]);
        }
        const input = field.querySelector(".extra-input");
        return selects.get(field).value() === 0 && !(input && input.value);
      });
    }
    function render() {
      const result = compose(model, fields.map(state), missing);
      const lines = result.lines;
      text = result.text;
      const marked = new Set(lines);
      const changedIndexes = previous ? lines.map((line, i) => previous.has(line) ? -1 : i).filter((i) => i >= 0) : [];
      const changed = new Set(changedIndexes.map((i) => lines[i]));
      previous = marked;
      pre.innerHTML = highlight(text, marked, changed);
      meta.textContent = count(text.length);
      changedIndexes.forEach((i) => replay(fields[i], "is-pulse"));
      if (resetButton) resetButton.disabled = isDefault();
    }
    section.addEventListener("input", (event) => {
      if (event.target.matches(".extra-input")) event.target.closest(".field").dataset.touched = "true";
      render();
    });
    fields.forEach((field) => field.addEventListener("animationend", (e) => { if (e.animationName === "label-pulse") field.classList.remove("is-pulse"); }));
    render();

    // Пустое обязательное значение: не отдаём промпт с меткой «не указано», а ведём к полю.
    function blocked() {
      const input = [...section.querySelectorAll(".extra-input:not([hidden])")].find((candidate) => !clean(candidate.value));
      if (!input) return false;
      const field = input.closest(".field");
      field.dataset.touched = "true";
      render();
      replay(input, "is-shake");
      toast("warn", "Заполните поле «" + field.querySelector(".label").textContent + "»");
      input.focus();
      return true;
    }
    function done(button, label, kind, message) {
      const text = button.querySelector(".btn-label");
      const original = button.dataset.label || (button.dataset.label = text.textContent);
      text.textContent = label;
      button.classList.toggle("is-done", kind === "ok");
      clearTimeout(button.timer);
      button.timer = setTimeout(() => { text.textContent = original; button.classList.remove("is-done"); }, 2200);
      toast(kind, message);
    }
    section.querySelector('[data-action="copy"]').addEventListener("click", (event) => {
      const button = event.currentTarget;
      if (blocked()) return;
      // writeText вызывается в обработчике клика: пока жива пользовательская активация.
      navigator.clipboard.writeText(text).then(
        () => done(button, "Скопировано", "ok", "Промпт скопирован: " + count(text.length)),
        () => {
          const range = document.createRange();
          range.selectNodeContents(pre);
          getSelection().removeAllRanges();
          getSelection().addRange(range);
          done(button, "Выделено", "warn", "Буфер обмена недоступен. Текст выделен: нажмите Ctrl+C");
        });
    });
    section.querySelector('[data-action="download"]').addEventListener("click", (event) => {
      if (blocked()) return;
      const url = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
      const link = Object.assign(document.createElement("a"), { href: url, download: section.dataset.file });
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      done(event.currentTarget, "Скачано", "ok", "Файл " + section.dataset.file + " скачан");
    });
    if (resetButton) resetButton.addEventListener("click", () => {
      fields.forEach((field) => {
        const param = model.params[field.dataset.index];
        if (param.choices) field.querySelectorAll("input").forEach((input) => { input.checked = param.default === "all"; });
        else {
          selects.get(field).set(0);
          const input = field.querySelector(".extra-input");
          const choice = field.querySelector(".extra-choice");
          if (input) input.value = "";
          if (choice) delete choice.dataset.for;
          delete field.dataset.touched;
        }
      });
      render();
      toast("ok", "Параметры сброшены");
    });
  });
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
