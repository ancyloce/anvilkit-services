// Development smoke helper: loads a built component module (the validator's
// artifacts/index.js and its stylesheets) in Playwright's Chromium headless
// shell on a synthetic origin served entirely through request interception
// (no socket, as the validator's browser check does), renders it through
// the validator's host bundles (one React, one ReactDOM, one Puck) with the
// given title, asserts that the title is visible, and writes one screenshot.
// It uses the validator package's own Playwright and host bundles and
// changes nothing there. Prints one JSON line: {visible, text, error?}.
//
//   node tools/smoke/hello-card-screenshot.mjs --validator <jobs/validator> \
//     --artifacts <validator out>/artifacts --component-json <source>/component.json \
//     --title "<title>" --out <screenshot.png>
import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";

function arg(name, def) {
	const i = process.argv.indexOf(name);
	return i >= 0 ? process.argv[i + 1] : def;
}

const validatorDir = path.resolve(arg("--validator"));
const artifacts = path.resolve(arg("--artifacts"));
const componentJson = path.resolve(arg("--component-json"));
const titleArg = arg("--title", "");
const out = path.resolve(arg("--out", "screenshot.png"));

const require = createRequire(path.join(validatorDir, "package.json"));
const { chromium } = require("playwright");

const hostDist = path.join(validatorDir, "fixtures", "host", "browser", "dist");
const hostFiles = ["react.js", "react-jsx-runtime.js", "react-dom.js", "react-dom-client.js", "puck.js"];
const component = JSON.parse(readFileSync(componentJson, "utf8"));
const cssFiles = readdirSync(artifacts).filter((f) => f.endsWith(".css"));
const firstTextField = (component.editableFields ?? []).find((f) => f.type === "text")?.name ?? "title";
// Without --title, the component's own default of its first text field is the expected text.
const title = titleArg || String((component.editableFields ?? []).find((f) => f.type === "text")?.default ?? "");
if (!title) {
	process.stdout.write(`${JSON.stringify({ visible: false, text: "", error: "no --title and no text field default in component.json" })}\n`);
	process.exit(1);
}
const puckType = component.puckType;

const origin = "https://smoke.anvilkit.invalid";
const indexHtml = `<!doctype html>
<html lang="en"><head><meta charset="utf-8" /><title>anvilkit smoke host</title>
<script type="importmap">{"imports":{"react":"/host/react.js","react/jsx-runtime":"/host/react-jsx-runtime.js","react-dom":"/host/react-dom.js","react-dom/client":"/host/react-dom-client.js","@puckeditor/core":"/host/puck.js"}}</script>
${cssFiles.map((f) => `<link rel="stylesheet" href="/component/${f}" />`).join("\n")}
<style>body { margin: 24px; font-family: system-ui, sans-serif; background: #ffffff; }</style>
</head><body><div id="root"></div><script type="module" src="/smoke.js"></script></body></html>`;
const smokeJs = `import { createElement } from "react";
import { createRoot } from "react-dom/client";
import { Render } from "@puckeditor/core";
import config from "/component/index.js";
const props = { ...(config.defaultProps ?? {}), ${JSON.stringify(firstTextField)}: ${JSON.stringify(title)} };
const root = createRoot(document.getElementById("root"));
root.render(createElement(Render, {
  config: { components: { ${JSON.stringify(puckType)}: config } },
  data: { content: [{ type: ${JSON.stringify(puckType)}, props: { id: "smoke-1", ...props } }], root: { props: {} } },
}));
window.__smokeMounted = true;
`;

const browser = await chromium.launch({ headless: true });
const report = { visible: false, text: "" };
try {
	const page = await browser.newPage({ viewport: { width: 900, height: 500 } });
	page.on("pageerror", (e) => { report.error = String(e); });
	await page.route(`${origin}/**`, async (route) => {
		const url = new URL(route.request().url());
		const p = url.pathname;
		const file = (dir, name, type) => route.fulfill({ status: 200, contentType: type, body: readFileSync(path.join(dir, name)) });
		if (p === "/") return route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: indexHtml });
		if (p === "/smoke.js") return route.fulfill({ status: 200, contentType: "text/javascript", body: smokeJs });
		if (p.startsWith("/host/") && hostFiles.includes(p.slice(6))) return file(hostDist, p.slice(6), "text/javascript");
		if (p === "/component/index.js") return file(artifacts, "index.js", "text/javascript");
		if (p.startsWith("/component/") && cssFiles.includes(p.slice(11))) return file(artifacts, p.slice(11), "text/css");
		return route.fulfill({ status: 404, contentType: "text/plain", body: "not served by the smoke host" });
	});
	await page.goto(`${origin}/`, { waitUntil: "load" });
	await page.waitForFunction(() => window.__smokeMounted === true, null, { timeout: 15_000 });
	const locator = page.getByText(title, { exact: true });
	await locator.first().waitFor({ state: "visible", timeout: 15_000 });
	report.visible = await locator.first().isVisible();
	report.text = (await locator.first().textContent()) ?? "";
	await page.screenshot({ path: out, fullPage: true });
	report.screenshot = out;
} catch (e) {
	report.error = String(e?.message ?? e);
} finally {
	await browser.close();
}
process.stdout.write(`${JSON.stringify(report)}\n`);
process.exit(report.visible ? 0 : 1);
