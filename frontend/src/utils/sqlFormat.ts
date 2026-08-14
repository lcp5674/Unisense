// 轻量 SQL 美化 —— 将单行/多行 SQL 重新排版为多行可读形式，便于两指标口径定义并排对比。
// 不改变语义与大小写（仅排版），字符串字面量整体保留避免误伤内部逗号/关键字。
// 支持：SELECT 列逐行缩进、FROM/WHERE/GROUP BY/ORDER BY/JOIN 等子句换行、
//       WHERE/ON 条件内 AND/OR 换行缩进、子查询括号缩进、函数调用括号保持同行。

type SqlToken =
  | { type: "word"; text: string }
  | { type: "string"; text: string }
  | { type: "comma" }
  | { type: "lparen" }
  | { type: "rparen" }
  | { type: "other"; text: string };

const isWordChar = (c: string) => /[A-Za-z0-9_$#.\u4e00-\u9fff]/.test(c);

/** 将 SQL 拆为 token 序列；' " ` 字面量整体作为一个 token */
function tokenizeSql(src: string): SqlToken[] {
  const tokens: SqlToken[] = [];
  let i = 0;
  while (i < src.length) {
    const ch = src[i];
    if (ch === "'" || ch === '"' || ch === "`") {
      const q = ch;
      let j = i + 1;
      while (j < src.length) {
        if (src[j] === "\\") {
          j += 2;
          continue;
        }
        if (src[j] === q) {
          j++;
          break;
        }
        j++;
      }
      tokens.push({ type: "string", text: src.slice(i, j) });
      i = j;
      continue;
    }
    if (ch === ",") {
      tokens.push({ type: "comma" });
      i++;
      continue;
    }
    if (ch === "(") {
      tokens.push({ type: "lparen" });
      i++;
      continue;
    }
    if (ch === ")") {
      tokens.push({ type: "rparen" });
      i++;
      continue;
    }
    if (/\s/.test(ch)) {
      i++;
      continue;
    }
    if (isWordChar(ch)) {
      let j = i;
      while (j < src.length && isWordChar(src[j])) j++;
      tokens.push({ type: "word", text: src.slice(i, j) });
      i = j;
      continue;
    }
    tokens.push({ type: "other", text: ch });
    i++;
  }
  return tokens;
}

/** 需要独立成行的 SQL 子句关键字（大写比对） */
const CLAUSE_KEYWORDS = new Set([
  "SELECT",
  "FROM",
  "WHERE",
  "HAVING",
  "LIMIT",
  "OFFSET",
  "UNION",
  "JOIN",
  "INNER",
  "LEFT",
  "RIGHT",
  "FULL",
  "CROSS",
  "ON",
  "GROUP",
  "ORDER",
  "BY",
  "VALUES",
  "SET",
  "UPDATE",
  "INSERT",
  "INTO",
  "DELETE",
]);

/** 前置关键字/行首 → 其后的 ( 视为子查询（而非函数调用） */
const CLAUSE_WORDS = new Set([
  "SELECT",
  "FROM",
  "WHERE",
  "HAVING",
  "JOIN",
  "ON",
  "AND",
  "OR",
  "GROUP",
  "ORDER",
  "UNION",
  "IN",
  "VALUES",
  "SET",
  "UPDATE",
  "INSERT",
  "INTO",
  "DELETE",
]);

const COND_JOINERS = new Set(["AND", "OR"]);

/**
 * 美化 SQL：输入任意空白形式的 SQL，输出规范多行。
 * 例：`SELECT a, b FROM t WHERE x = 1 AND y = 2 GROUP BY a`
 * →
 * ```
 * SELECT
 *   a,
 *   b
 * FROM t
 * WHERE x = 1
 *   AND y = 2
 * GROUP BY a
 * ```
 */
export function formatSql(sql: string): string {
  const src = String(sql ?? "").trim();
  if (!src) return src;

  const tokens = tokenizeSql(src);
  const lines: string[] = [];
  let depth = 0; // 子查询括号层级
  let inSelect = false; // SELECT 与 FROM 之间（列列表逗号换行、缩进 1）
  let inCond = false; // 条件区（AND/OR 换行、缩进 1）
  let condOpen = false; // WHERE/ON/HAVING 之后，允许 AND/OR 换行
  let selectPending = false; // SELECT 独占一行：首列 add 前先 flush "SELECT" 行再开启列缩进
  const parenStack: Array<"sub" | "func"> = []; // 括号栈：区分子查询/函数调用
  let line = "";
  let prevWord = "";

  const indentFor = () => "  ".repeat(depth + (inSelect ? 1 : 0) + (inCond ? 1 : 0));
  const flush = () => {
    const t = line.trim();
    if (t) lines.push(indentFor() + t);
    line = "";
  };
  const add = (txt: string) => {
    if (selectPending) {
      flush(); // "SELECT" 行：此时 inSelect 仍 false → 缩进 0
      inSelect = true; // 后续列行缩进 1
      selectPending = false;
    }
    if (!line) {
      line = txt;
      return;
    }
    // "(" 或紧跟在 "(" 后的内容不加前导空格（函数调用括号紧贴）
    if (txt === "(" || line.endsWith("(")) {
      line += txt;
      return;
    }
    line += ` ${txt}`;
  };

  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    if (t.type === "word") {
      const up = t.text.toUpperCase();
      const next = tokens[i + 1];
      const nextUp = next && next.type === "word" ? next.text.toUpperCase() : "";

      // 复合关键字：GROUP BY / ORDER BY / LEFT JOIN 等
      if ((up === "GROUP" || up === "ORDER") && nextUp === "BY") {
        flush();
        line = `${t.text} BY`;
        i++;
        inSelect = false;
        inCond = false;
        condOpen = false;
        prevWord = "CLAUSE";
        continue;
      }
      if (
        (up === "INNER" || up === "LEFT" || up === "RIGHT" || up === "FULL" || up === "CROSS") &&
        nextUp === "JOIN"
      ) {
        flush();
        line = `${t.text} JOIN`;
        i++;
        inSelect = false;
        inCond = false;
        condOpen = false;
        prevWord = "CLAUSE";
        continue;
      }

      if (up === "SELECT") {
        flush();
        line = t.text;
        inSelect = false; // 暂不开启；首列 add 时再开启，保证 "SELECT" 行缩进 0
        selectPending = true;
        inCond = false;
        condOpen = false;
        prevWord = "CLAUSE";
        continue;
      }
      if (up === "FROM") {
        flush();
        line = t.text;
        inSelect = false;
        inCond = false;
        condOpen = false;
        prevWord = "CLAUSE";
        continue;
      }
      if (up === "WHERE" || up === "ON" || up === "HAVING") {
        flush();
        line = t.text;
        inSelect = false;
        inCond = false;
        condOpen = true;
        prevWord = "CLAUSE";
        continue;
      }
      if (CLAUSE_KEYWORDS.has(up)) {
        flush();
        line = t.text;
        inSelect = false;
        inCond = false;
        condOpen = false;
        prevWord = "CLAUSE";
        continue;
      }
      if (COND_JOINERS.has(up) && condOpen) {
        flush(); // WHERE/ON 行：此时 inCond 仍为 false → 缩进 0
        inCond = true; // AND/OR 行及其后条件：缩进 1
        line = t.text;
        prevWord = "CLAUSE";
        continue;
      }
      // 普通标识符（列名、表名、AS、DISTINCT、函数名…）
      add(t.text);
      prevWord = up;
      continue;
    }
    if (t.type === "comma") {
      line += ",";
      // 仅 SELECT 列区且不在函数括号内时换行（子查询括号内允许）
      if (inSelect && !parenStack.includes("func")) flush();
      prevWord = "";
      continue;
    }
    if (t.type === "lparen") {
      const isSubquery =
        line.trim() === "" || prevWord === "CLAUSE" || CLAUSE_WORDS.has(prevWord);
      if (isSubquery) {
        parenStack.push("sub");
        if (line && !line.endsWith(" ") && !line.endsWith("(")) line += " ";
        add("(");
        flush(); // 括号行（如 FROM (）保持原层级缩进
        depth++; // 括号内部行再缩进 1
      } else {
        parenStack.push("func");
        add("(");
      }
      prevWord = "";
      continue;
    }
    if (t.type === "rparen") {
      const kind = parenStack.pop() ?? "func";
      line = line.trimEnd() + ")";
      if (kind === "sub") {
        flush(); // 子查询末行（含 )）保持内部缩进
        if (depth > 0) depth--; // 括号结束，归位外层层级
      }
      prevWord = "";
      continue;
    }
    // string / other（运算符、数字等）
    add(t.text);
    if (t.type === "string") prevWord = "STR";
  }
  flush();
  return lines.join("\n");
}
