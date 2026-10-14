// Сверка страницы со сборкой: все встроенные скрипты страницы компилируются и перечислены в политике CSP по хешу, а
// логика сборки текста даёт тот же текст, что build.py. Модель сценария строится так же, как на странице: из данных
// и текста <pre>. Запуск, как в CI (<tmp> — каталог вне репозитория):
//   python3 scripts/build.py --check --site <tmp>/site --parity-cases <tmp>/cases.json
//   node scripts/parity.mjs <tmp>/site/index.html <tmp>/cases.json
// Случаи: все параметры по умолчанию; каждый вариант каждого параметра при остальных по умолчанию (с вводом,
// в том числе с разными пробельными символами); полный перебор сочетаний, где их мало; все параметры не по умолчанию.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SHOWN = 10; // сколько расхождений печатать подробно

function die(message) {
  console.error(`parity: ${message}`);
  process.exit(1);
}

const [pagePath, casesPath] = process.argv.slice(2);
if (!pagePath || !casesPath) die("запуск: node scripts/parity.mjs <страница index.html> <cases.json>");
function readText(path) {
  try {
    return readFileSync(path, "utf8");
  } catch (error) {
    return die(`${path}: не прочитать (${error.code ?? error.message})`);
  }
}

const page = readText(pagePath);

// Встроенные скрипты: исполняемые должны компилироваться, JSON-данные и прочие типы пропускаются.
const scripts = [...page.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)].map(([, attrs, code], i) => {
  const id = attrs.match(/\bid\s*=\s*["']?([\w-]+)/i)?.[1];
  return {
    code,
    id,
    type: (attrs.match(/\btype\s*=\s*["']?([^\s"'>]*)/i)?.[1] ?? "").toLowerCase(),
    src: /\bsrc\s*=/i.test(attrs),
    label: `<script #${i + 1}${id ? ` id="${id}"` : ""}>`,
  };
});
const executable = scripts.filter((s) => !s.src && ["", "text/javascript", "application/javascript"].includes(s.type));
const modules = scripts.filter((s) => !s.src && s.type === "module");
if (modules.length) die(`${modules[0].label}: type="module" — сверка умеет проверять только классические скрипты`);
if (!executable.length) die(`${pagePath}: нет ни одного исполняемого встроенного скрипта`);
const broken = [];
for (const script of executable) {
  try {
    new vm.Script(script.code, { filename: script.label });
  } catch (error) {
    broken.push(`${script.label}: синтаксическая ошибка: ${error.message}\n${String(error.stack).split("\n").slice(0, 4).join("\n")}`);
  }
}
if (broken.length) die(broken.join("\n"));

// Политика CSP: каждый встроенный исполняемый скрипт и стиль указан хешем, небезопасных источников нет.
const policy = page.match(/<meta http-equiv="Content-Security-Policy" content="([^"]*)">/)?.[1]
  .replaceAll("&#x27;", "'").replaceAll("&amp;", "&") ?? die(`${pagePath}: нет <meta http-equiv="Content-Security-Policy">`);
const sources = (directive) => policy.split(";").map((part) => part.trim().split(/\s+/)).find(([name]) => name === directive)?.slice(1) ?? [];
// Политика действует только на то, что идет после нее: <meta> стоит в <head> раньше любого скрипта и стиля.
const metaAt = page.search(/<meta http-equiv="Content-Security-Policy"/);
const firstCode = page.search(/<(?:script|style)\b/i);
if (metaAt < page.search(/<head\b/i) || metaAt > page.indexOf("</head>") || metaAt > firstCode) die("CSP: <meta> должен стоять в <head> раньше первого <script> и <style>");
if (/unsafe-/.test(policy)) die(`CSP разрешает небезопасное: ${policy}`);
const hash = (code) => `'sha256-${createHash("sha256").update(code).digest("base64")}'`;
for (const script of executable) {
  if (!sources("script-src").includes(hash(script.code))) die(`CSP: хеша ${script.label} нет в script-src: ${policy}`);
}
for (const [, code] of page.matchAll(/<style\b[^>]*>([\s\S]*?)<\/style>/gi)) {
  if (!sources("style-src").includes(hash(code))) die(`CSP: хеша <style> нет в style-src: ${policy}`);
}
if (sources("script-src").length !== executable.length) die(`CSP: в script-src ${sources("script-src").length} хешей, скриптов ${executable.length}`);
if (sources("default-src").join() !== "'none'") die(`CSP: default-src должен быть 'none': ${policy}`);

const logic = scripts.find((s) => s.id === "logic")?.code ?? die(`${pagePath}: нет скрипта <script id="logic">`);
const dataText = scripts.find((s) => s.id === "data")?.code ?? die(`${pagePath}: нет данных <script id="data">`);
let data;
let cases;
try {
  data = JSON.parse(dataText);
} catch (error) {
  die(`${pagePath}: данные страницы не разбираются как JSON: ${error.message}`);
}
try {
  cases = JSON.parse(readText(casesPath));
} catch (error) {
  die(`${casesPath}: не разбирается как JSON: ${error.message}`);
}
const { model: modelOf, compose } = vm.runInNewContext(`${logic}\nplaybook`, {}, { filename: "<script id=\"logic\">" });

// Текст по умолчанию каждого сценария — содержимое его <pre>, как его читает страница (textContent). В разметке
// экранированы только <, > и &; любая другая сущность значила бы, что разбор здесь разошелся с браузером.
const unescape = (text) => text.replace(/&(lt|gt|amp);|&[#\w]+;/g, (entity, name) => ({ lt: "<", gt: ">", amp: "&" })[name] ?? die(`<pre>: неожиданная сущность ${entity}`));
const texts = new Map([...page.matchAll(/<section class="scenario" id="([^"]+)"[\s\S]*?<pre class="prompt"[^>]*>([\s\S]*?)<\/pre>/g)]
  .map(([, id, text]) => [id, unescape(text.replace(/^\n/, ""))])); // браузер отбрасывает один перевод строки сразу после <pre>
const models = new Map(data.scenarios.map((scenario) => {
  const text = texts.get(scenario.id) ?? die(`${pagePath}: у сценария ${scenario.id} нет <pre> с текстом`);
  return [scenario.id, { model: modelOf(scenario, text), text }];
}));
if (!models.size) die(`${pagePath}: на странице нет сценариев`);
if (!Array.isArray(cases) || !cases.length) die(`${casesPath}: нет ни одного случая`);
const perScenario = new Map();
cases.forEach((item, i) => {
  if (!models.has(item?.id)) die(`${casesPath}, случай ${i}: неизвестный сценарий ${JSON.stringify(item?.id)}; на странице: ${[...models.keys()].join(", ")}`);
  if (!Array.isArray(item.states) || typeof item.expected !== "string") die(`${casesPath}, случай ${i}: нужны states (массив) и expected (строка)`);
  perScenario.set(item.id, (perScenario.get(item.id) ?? 0) + 1);
});
const uncovered = [...models.keys()].filter((id) => !perScenario.has(id));
if (uncovered.length) die(`${casesPath}: нет случаев для сценариев страницы: ${uncovered.join(", ")}`);

// Индекс первого различия в единицах UTF-16 (как у slice и индексов строк), до длины большей строки.
function firstDifference(a, b) {
  const shared = Math.min(a.length, b.length);
  let i = 0;
  while (i < shared && a[i] === b[i]) i += 1;
  return i;
}

const around = (text, at) => JSON.stringify(text.slice(Math.max(0, at - 30), at + 50));
let failed = 0;
let defaultsFailed = 0;
// Состояния по умолчанию из данных дают текст, который страница показывает без JS.
for (const [id, { model, text }] of models) {
  let built;
  try {
    ({ text: built } = compose(model, model.defaults, data.missing));
  } catch (error) {
    built = `<ошибка: ${error.message}>`;
  }
  if (built !== text) {
    defaultsFailed += 1;
    const at = firstDifference(built, text);
    console.error(`${id}: состояния по умолчанию из данных дают другой текст, чем <pre> (с позиции ${at}):\n  данные: ${around(built, at)}\n  <pre>:  ${around(text, at)}`);
  }
}
for (const { id, states, expected } of cases) {
  const { model } = models.get(id);
  if (states.length !== model.params.length) die(`${casesPath}: у случая ${id} ${states.length} состояний, параметров ${model.params.length}`);
  let text;
  try {
    ({ text } = compose(model, states, data.missing));
  } catch (error) {
    text = `<ошибка: ${error.message}>`;
  }
  if (text !== expected) {
    failed += 1;
    if (failed <= SHOWN) {
      const at = firstDifference(text, expected);
      console.error(`${id} ${JSON.stringify(states)}: расхождение с позиции ${at} (длина страницы ${text.length}, build.py ${expected.length}):\n  страница: ${around(text, at)}\n  build.py: ${around(expected, at)}`);
    }
  }
}
if (failed > SHOWN) console.error(`... и еще ${failed - SHOWN} расхождений`);
console.log(`сверка страницы с build.py: ${cases.length - failed}/${cases.length} совпадают (и ${models.size} текстов по умолчанию); скриптов проверено: ${executable.length}; сценариев: ${models.size}`);
process.exit(failed || defaultsFailed ? 1 : 0);
