// Сверка страницы со сборкой: все встроенные скрипты страницы компилируются, а логика сборки текста даёт тот же
// текст, что build.py. Запуск, как в CI (<tmp> — каталог вне репозитория):
//   python3 scripts/build.py --check --site <tmp>/site --parity-cases <tmp>/cases.json
//   node scripts/parity.mjs <tmp>/site/index.html <tmp>/cases.json
// Случаи: все параметры по умолчанию; каждый вариант каждого параметра при остальных по умолчанию (с вводом,
// в том числе с разными пробельными символами); полный перебор сочетаний, где их мало; все параметры не по умолчанию.
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
const { compose } = vm.runInNewContext(`${logic}\nplaybook`, {}, { filename: "<script id=\"logic\">" });

// Пустой или неполный набор случаев сверкой не считается.
const models = new Map(data.scenarios.map((scenario) => [scenario.id, scenario]));
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
for (const { id, states, expected } of cases) {
  const model = models.get(id);
  const full = model.params.map((param, i) => states[i] ?? (param.choices
    ? { picked: param.default === "all" ? [...param.choices] : [] }
    : { index: 0 }));
  let text;
  try {
    ({ text } = compose(model, full, data.missing));
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
console.log(`сверка страницы с build.py: ${cases.length - failed}/${cases.length} совпадают; скриптов проверено: ${executable.length}; сценариев: ${models.size}`);
process.exit(failed ? 1 : 0);
