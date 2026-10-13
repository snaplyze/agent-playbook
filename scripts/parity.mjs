// Сверка логики страницы с Python: каждый вариант параметров страница собирает в тот же текст, что build.py.
// Запуск: python3 scripts/build.py --site _site --parity-cases cases.json && node scripts/parity.mjs _site/index.html cases.json
import { readFileSync } from "node:fs";
import vm from "node:vm";

const [pagePath, casesPath] = process.argv.slice(2);
const page = readFileSync(pagePath, "utf8");
const logic = page.match(/<script id="logic">([\s\S]*?)<\/script>/)[1];
const data = JSON.parse(page.match(/<script type="application\/json" id="data">([\s\S]*?)<\/script>/)[1]);
const { compose } = vm.runInNewContext(`${logic}\nplaybook`);
const cases = JSON.parse(readFileSync(casesPath, "utf8"));

let failed = 0;
for (const { id, states, expected } of cases) {
  const model = data.scenarios.find((scenario) => scenario.id === id);
  const full = model.params.map((param, i) => states[i] ?? (param.choices
    ? { picked: param.default === "all" ? [...param.choices] : [] }
    : { index: 0 }));
  const { text } = compose(model, full, data.missing);
  if (text !== expected) {
    failed += 1;
    const at = [...text].findIndex((char, i) => char !== expected[i]);
    console.error(`${id} ${JSON.stringify(states)}: расхождение с позиции ${at}:\n  страница: ${JSON.stringify(text.slice(at, at + 80))}\n  build.py: ${JSON.stringify(expected.slice(at, at + 80))}`);
  }
}
console.log(`сверка страницы с build.py: ${cases.length - failed}/${cases.length} совпадают`);
process.exit(failed ? 1 : 0);
