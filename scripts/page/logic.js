// Сборка текста промпта: та же логика, что param_text и assemble в scripts/build.py.
// CI сверяет их (scripts/parity.mjs): каждый вариант каждого параметра при остальных по умолчанию, сочетания
// параметров и ввод с разными пробельными символами.
var playbook = (() => {
  // Пробельные символы те же, что в WHITESPACE в build.py: сборка подставляет их на место метки.
  const spaces = /[@WHITESPACE@]+/g;
  const clean = (value) => value.replace(spaces, " ").replace(/^ | $/g, "");
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
