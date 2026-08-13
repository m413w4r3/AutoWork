const assert = require("node:assert/strict");

require("../extension/final-output.js");

const { createAccumulator, outputChars } = globalThis.ChatGPTBridgeFinalOutput;

const rewritten = createAccumulator();
rewritten.observe("ABC");
rewritten.observe("ABCDE");
rewritten.observe("ABXYZ");
assert.equal(rewritten.final(), "ABXYZ");
assert.notEqual(rewritten.final(), "ABCDEXYZ");
assert.equal(outputChars("A😀B"), 3);

const fiveSubjects = createAccumulator();
fiveSubjects.observe(
  "# SUJETS CANDIDATS\n\n## SUBJECT S1\nA\n\n## SUBJECT S2\nB",
);
fiveSubjects.observe(
  [
    "# SUJETS CANDIDATS",
    "## SUBJECT S1\nA réécrit",
    "## SUBJECT S2\nB réécrit",
    "## SUBJECT S3\nC",
    "## SUBJECT S4\nD",
    "## SUBJECT S5\nE",
  ].join("\n\n"),
);
const finalReport = fiveSubjects.final();
assert.equal((finalReport.match(/^## SUBJECT S\d+$/gm) || []).length, 5);
assert.equal(finalReport.includes("A\n\n## SUBJECT S2\nB"), false);

console.log("final-only output contract: ok");
