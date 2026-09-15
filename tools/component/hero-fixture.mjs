#!/usr/bin/env node
// S1-T01/S1-T02 (development plan 2026-09-12): pin the fixed Hero fixture and build its two outputs.
//
//   node tools/component/hero-fixture.mjs support   # build the support packages the protected commands resolve
//   node tools/component/hero-fixture.mjs pin       # source manifest + build-input inventory
//   node tools/component/hero-fixture.mjs build     # protected commands, npm tarball, browser ESM/CSS, manifests
//   node tools/component/hero-fixture.mjs facades   # host facade modules for the Studio checkout
//   node tools/component/hero-fixture.mjs all       # support, pin, facades, build
//   node tools/component/hero-fixture.mjs compare   # rebuilt output (ANVILKIT_HERO_OUTPUT) against the durable record
//
// The fixture is @anvilkit/hero at the component repository commit checked out as the Studio submodule
// (ANVILKIT_STUDIO_ROOT/packages/extensions/components). At that commit the component repository carries no
// lockfile of its own: its packages are importers of the Studio root workspace, whose pnpm-lock.yaml is the
// dependency lock, so every protected command runs from the Studio root with --filter. The component
// repository is never modified; the browser build reads a config from the output directory and the only
// side effect inside the checkout is the package's own dist/, which its build script owns. pnpm 12 writes a
// nested pnpm-lock.yaml into the component checkout whenever a script runs there; it is removed again.
//
// The protected commands resolve four workspace packages through their built dist, which a clean clone does
// not carry: @anvilkit/vitest-config and @anvilkit/ui, and — for the protected contract suites, whose src/button
// import reaches @anvilkit/analytics-react — @anvilkit/analytics-core and @anvilkit/analytics-react (B11, closed
// 2026-09-13). `support` builds them in dependency order from the pinned lock and `all` runs it first, so a rebuild
// from a fresh clone (whole workspace installed frozen) needs no manual step; `pin` records the analytics packages
// and their dist beside the other support packages and `compare` holds them to the record.
//
// Everything written goes to ANVILKIT_HERO_OUTPUT (default outputs/hero-fixture-v<version>, git-ignored) and,
// for the facades, to the Studio checkout's public/host/<hostProfileId>/facades directory. This tool builds
// and records; it publishes nothing and qualifies nothing.
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STUDIO = process.env.ANVILKIT_STUDIO_ROOT || "/root/Rhett/anvilkit-studio";
const COMPONENTS = process.env.ANVILKIT_COMPONENTS_ROOT || join(STUDIO, "packages/extensions/components");
const HERO = join(COMPONENTS, "src/hero");
const HOST_PROFILE_ID = "studio-host-v1";
const RULES = JSON.parse(readFileSync(join(REPO, "docs/archive/contracts/components/facade-rewrite-rules-v1.json"), "utf8"));
const heroManifest = JSON.parse(readFileSync(join(HERO, "package.json"), "utf8"));
const OUT = process.env.ANVILKIT_HERO_OUTPUT || join(REPO, "outputs", `hero-fixture-v${heroManifest.version}`);

const sha = (buf) => "sha256:" + createHash("sha256").update(buf).digest("hex");
const fileRecord = (root, rel) => {
  const buf = readFileSync(join(root, rel));
  return { path: rel.split("\\").join("/"), digest: sha(buf), sizeBytes: String(buf.length), objectVersion: "v1" };
};
const walk = (root, dir = "", out = []) => {
  for (const e of readdirSync(join(root, dir), { withFileTypes: true })) {
    const rel = dir ? `${dir}/${e.name}` : e.name;
    if (e.name === "node_modules" || e.name === "dist" || e.name === ".turbo") continue;
    if (e.isDirectory()) walk(root, rel, out); else out.push(rel);
  }
  return out.sort();
};
const git = (cwd, ...args) => spawnSync("git", args, { cwd, encoding: "utf8" }).stdout.trim();
const run = (cmd, args, cwd, label) => {
  const p = spawnSync(cmd, args, { cwd, encoding: "utf8", env: { ...process.env, CI: "1", FORCE_COLOR: "0" } });
  const status = p.status === 0 ? "PASS" : "FAIL";
  console.log(`${status}  ${label}`);
  if (p.status !== 0) console.log((p.stdout + p.stderr).trim().split("\n").slice(-12).map((l) => "      | " + l).join("\n"));
  // pnpm 12 leaves a nested lockfile behind when a script runs inside the component checkout; the checkout
  // carries none at this commit, so it is removed to leave the submodule exactly as it was.
  const stray = join(COMPONENTS, "pnpm-lock.yaml");
  if (existsSync(stray) && git(COMPONENTS, "ls-files", "pnpm-lock.yaml") === "") rmSync(stray);
  return { label, status, exitCode: p.status, output: (p.stdout + p.stderr).trim().slice(-4000) };
};
const writeJson = (p, v) => { mkdirSync(dirname(p), { recursive: true }); writeFileSync(p, JSON.stringify(v, null, 2) + "\n"); };
const version = (pkgDir) => JSON.parse(readFileSync(join(pkgDir, "package.json"), "utf8")).version;

function pin() {
  mkdirSync(OUT, { recursive: true });
  const componentCommit = git(COMPONENTS, "rev-parse", "HEAD");
  const dirty = git(COMPONENTS, "status", "--short");
  const studioCommit = git(STUDIO, "rev-parse", "HEAD");
  const files = walk(HERO).map((rel) => fileRecord(HERO, rel));
  const sourceDigest = sha(Buffer.from(files.map((f) => `${f.path}\n${f.digest}\n`).join(""), "utf8"));
  const source = {
    schemaVersion: 1,
    identity: { tenantId: "team-fixture", componentId: "cmp-hero-fixture", draftId: "draft-hero-fixture", packageName: heroManifest.name, packageVersion: heroManifest.version, puckType: "Hero" },
    baseRevision: `anvilkit-components:${componentCommit}`,
    files,
    sourceDigest,
  };
  writeJson(join(OUT, "source-manifest.json"), source);
  const supportDirs = ["packages/tooling/configs/tailwind", "packages/tooling/configs/typescript", "packages/tooling/configs/biome", "packages/tooling/configs/vitest", "packages/runtime/ui", ...ANALYTICS_DIRS];
  const supportFiles = [];
  for (const d of supportDirs) {
    const root = join(STUDIO, d);
    if (!existsSync(root)) { supportFiles.push({ path: d, missing: true }); continue; }
    // the UI package contributes only its three consumed sources; the config packages are recorded whole; the
    // analytics packages are submodules and contribute their tracked files (no gitlink, no ignored check output)
    const entries = ANALYTICS_DIRS.includes(d) ? git(root, "ls-files").split("\n").filter(Boolean).sort() : walk(root);
    for (const rel of entries) if (!d.endsWith("/ui") || !rel.startsWith("src/") || /^src\/(button|rainbow-button|lib\/utils)\.tsx?$/.test(rel)) supportFiles.push({ ...fileRecord(root, rel), path: `${d}/${rel}` });
  }
  const uiDist = ["dist/button.js", "dist/lib/utils.js", "dist/rainbow-button.js", "dist/index.js"].filter((f) => existsSync(join(STUDIO, "packages/runtime/ui", f))).map((f) => ({ ...fileRecord(join(STUDIO, "packages/runtime/ui"), f), path: `packages/runtime/ui/${f}` }));
  // the built analytics packages the contract suites resolve (every dist file but the source maps)
  const analyticsDist = ANALYTICS_DIRS.flatMap((d) => (existsSync(join(STUDIO, d, "dist")) ? walk(join(STUDIO, d), "dist").filter((f) => !f.endsWith(".map")).map((f) => ({ ...fileRecord(join(STUDIO, d), f), path: `${d}/${f}` })) : []));
  const componentRepoFiles = ["package.json", "pnpm-workspace.yaml", "biome.json", "postcss.config.mjs", "tsconfig.json", "vitest.config.mts", "turbo/generators/config.ts", "turbo/generators/run-component-generator.mjs"].filter((f) => existsSync(join(COMPONENTS, f))).map((f) => fileRecord(COMPONENTS, f));
  const patches = existsSync(join(COMPONENTS, "patches")) ? readdirSync(join(COMPONENTS, "patches")).sort().map((f) => fileRecord(COMPONENTS, `patches/${f}`)) : [];
  const templates = existsSync(join(COMPONENTS, "turbo/generators/templates")) ? walk(join(COMPONENTS, "turbo/generators/templates")).map((f) => fileRecord(join(COMPONENTS, "turbo/generators/templates"), f)) : [];
  const protectedTests = existsSync(join(COMPONENTS, "tests")) ? walk(join(COMPONENTS, "tests")).map((f) => fileRecord(join(COMPONENTS, "tests"), f)) : [];
  const inventory = {
    schemaVersion: 1,
    recordedAt: new Date().toISOString(),
    fixture: { packageName: heroManifest.name, packageVersion: heroManifest.version, puckType: "Hero", sourceDigest, sourceManifest: "source-manifest.json" },
    componentRepository: { url: git(COMPONENTS, "remote", "get-url", "origin"), commit: componentCommit, dirty: dirty === "" ? [] : dirty.split("\n"), dd04Pin: "5e9d3059df0c2d74ebfa4b4d06f6fe8b6a994519 (scoped read 2026-09-07; historical evidence, not this fixture)", lockfileInRepository: git(COMPONENTS, "ls-files", "pnpm-lock.yaml") !== "" },
    studioRepository: { url: git(STUDIO, "remote", "get-url", "origin"), commit: studioCommit, workspaceIncludes: "packages/extensions/components/src/* (root pnpm-workspace.yaml)", lockfile: fileRecord(STUDIO, "pnpm-lock.yaml"), workspaceDefinition: fileRecord(STUDIO, "pnpm-workspace.yaml") },
    toolchain: { node: process.version, pnpm: JSON.parse(readFileSync(join(STUDIO, "package.json"), "utf8")).packageManager, rslib: version(join(COMPONENTS, "node_modules/@rslib/core")), rsbuildPluginReact: version(join(COMPONENTS, "node_modules/@rsbuild/plugin-react")), typescript: version(join(COMPONENTS, "node_modules/typescript")), react: version(join(STUDIO, "apps/studio/node_modules/react")), reactDom: version(join(STUDIO, "apps/studio/node_modules/react-dom")), puck: version(join(STUDIO, "apps/studio/node_modules/@puckeditor/core")), anvilkitUi: version(join(STUDIO, "packages/runtime/ui")), next: version(join(STUDIO, "apps/studio/node_modules/next")), platform: `${process.platform}-${process.arch}` },
    componentRepositoryFiles: componentRepoFiles,
    generatorTemplates: templates,
    protectedTests,
    patches: { files: patches, wired: false, note: "the four patch files are not referenced by any patchedDependencies entry (DD-04 #protected-command-set); recorded as inert" },
    supportClosure: { files: supportFiles, uiDist, analyticsDist },
    protectedCommands: [
      { name: "build", command: "pnpm --filter @anvilkit/hero build", enforces: "rslib build --lib esm && rslib build --lib cjs --no-clean; bundleless dual output plus dist/styles.css" },
      { name: "typecheck", command: "pnpm --filter @anvilkit/hero typecheck", enforces: "tsc --noEmit" },
      { name: "publint", command: "pnpm --filter @anvilkit/hero exec publint", enforces: "export map correctness" },
      { name: "size-limit", command: "pnpm --filter @anvilkit/hero exec size-limit", enforces: "30 KB gzip dist/index.js excluding peers; 12 KB gzip dist/styles.css" },
      { name: "lint", command: "pnpm --filter anvilkit-components lint", enforces: "shared Biome config resolved, biome lint ." },
      { name: "check:fields-drift", command: "pnpm --filter anvilkit-components check:fields-drift", enforces: "generated field goldens match UI source" },
      { name: "test", command: "pnpm --filter anvilkit-components exec vitest run", enforces: "authoring contract and parity, build-graph contract, drift self-test, four-locale key equality", requires: "@anvilkit/analytics-core and @anvilkit/analytics-react built from the pinned lock (src/button, imported by four suite files, resolves @anvilkit/analytics-react only through its dist); see supportBuilds in build-record.json" },
    ],
    supportBuilds: SUPPORT_BUILDS.map((b) => b.command),
    notes: [
      "The protected contract suites need the built @anvilkit/analytics-core and @anvilkit/analytics-react (B11): both packages and their dist are recorded in supportClosure and built by the support step, whose manifests must agree with the pinned Studio lockfile (frozen install of the whole workspace).",
      "Component styles.css compiles Tailwind v4 utilities into dist/styles.css together with ordinary CSS (DD-05 §6.3 separation is not performed by this fixture; recorded for the DD-04 freeze).",
      "The Hero source imports @anvilkit/ui subpaths (button, lib/utils, rainbow-button); the facade rules' @anvilkit/ui entry must list those subpaths (S1-T03 record).",
      "Type-only imports from @puckeditor/core and react are erased by the build; the browser bundle imports react, react/jsx-runtime and the three @anvilkit/ui subpaths.",
    ],
  };
  writeJson(join(OUT, "inventory.json"), inventory);
  console.log(`pinned ${heroManifest.name}@${heroManifest.version} at ${componentCommit}${dirty ? " (DIRTY: " + dirty + ")" : ""}; ${files.length} source files; sourceDigest ${sourceDigest}`);
  return inventory;
}

const ANALYTICS_DIRS = ["packages/capabilities/analytics/core", "packages/capabilities/analytics/react"];
// Built in this order from the Studio root: the config preset the suites load, the UI package the Hero imports, then
// the analytics pair (react depends on core) that src/button resolves through dist.
const SUPPORT_BUILDS = ["@anvilkit/vitest-config", "@anvilkit/ui", "@anvilkit/analytics-core", "@anvilkit/analytics-react"].map((name) => ({ name, command: `pnpm --filter ${name} build` }));

function support() {
  const results = SUPPORT_BUILDS.map((b) => run("pnpm", ["--filter", b.name, "build"], STUDIO, `support build ${b.name}`));
  const missing = ANALYTICS_DIRS.filter((d) => !existsSync(join(STUDIO, d, "dist/index.js")));
  if (missing.length) console.log(`FAIL  analytics dist missing after the support builds: ${missing.join(", ")}`);
  const failed = results.filter((r) => r.status !== "PASS").length + missing.length;
  console.log(`${results.length} support builds, ${failed} failures`);
  if (failed) process.exit(1);
  return results;
}

function build(supportBuilds = []) {
  mkdirSync(join(OUT, "npm"), { recursive: true });
  const results = [];
  results.push(run("pnpm", ["--filter", "@anvilkit/hero", "build"], STUDIO, "protected build (rslib esm + cjs)"));
  results.push(run("pnpm", ["--filter", "@anvilkit/hero", "typecheck"], STUDIO, "protected typecheck"));
  results.push(run("pnpm", ["--filter", "@anvilkit/hero", "exec", "publint"], STUDIO, "protected publint"));
  results.push(run("pnpm", ["--filter", "@anvilkit/hero", "exec", "size-limit"], STUDIO, "protected size-limit"));
  results.push(run("pnpm", ["--filter", "anvilkit-components", "lint"], STUDIO, "protected lint"));
  results.push(run("pnpm", ["--filter", "anvilkit-components", "check:fields-drift"], STUDIO, "protected check:fields-drift"));
  results.push(run("pnpm", ["--filter", "anvilkit-components", "exec", "vitest", "run"], STUDIO, "protected contract suites (vitest)"));
  // npm tarball from the built package
  for (const f of readdirSync(join(OUT, "npm"))) rmSync(join(OUT, "npm", f));
  results.push(run("pnpm", ["--filter", "@anvilkit/hero", "pack", "--pack-destination", join(OUT, "npm")], STUDIO, "npm tarball (pnpm pack)"));
  const tarballName = readdirSync(join(OUT, "npm")).find((f) => f.endsWith(".tgz"));
  const tarball = tarballName ? fileRecord(join(OUT, "npm"), tarballName) : null;
  const npmFiles = ["dist/index.js", "dist/index.cjs", "dist/index.d.ts", "dist/styles.css", "i18n/messages/en.json", "i18n/messages/zh.json", "i18n/messages/ja.json", "i18n/messages/ko.json"].filter((f) => existsSync(join(HERO, f))).map((f) => fileRecord(HERO, f));
  // browser build from the same source; host-shared packages external, rewritten to facades afterwards
  const configPath = join(OUT, "hero.browser.rslib.config.mjs");
  const externals = RULES.facades.map((f) => f.bareSpecifier);
  writeFileSync(configPath, `// Generated by tools/component/hero-fixture.mjs: browser (Studio remote module) build of ${heroManifest.name} from the
// same source as the npm package. Host-shared packages stay external and are rewritten to host facade URLs
// afterwards (contracts/components/facade-rewrite-rules-v1.json); everything else is bundled.
import { pluginReact } from ${JSON.stringify(join(COMPONENTS, "node_modules/@rsbuild/plugin-react/dist/index.js"))};
import { defineConfig } from ${JSON.stringify(join(COMPONENTS, "node_modules/@rslib/core/dist/index.js"))};
export default defineConfig({
  source: { entry: { hero: "./src/index.ts" } },
  lib: [{ id: "browser", bundle: true, format: "esm", dts: false, autoExternal: false,
          output: { distPath: { root: ${JSON.stringify(join(OUT, "browser"))} }, filename: { js: "[name].esm.js", css: "[name].css" } } }],
  output: { target: "web", minify: false, cleanDistPath: true,
            externals: [${externals.map((s) => JSON.stringify(s)).join(", ")}, /^react\\//, /^react-dom\\//, /^@anvilkit\\/ui\\//] },
  performance: { buildCache: false },
  plugins: [pluginReact()],
});
`);
  results.push(run("node", [join(COMPONENTS, "node_modules/@rslib/core/bin/rslib.js"), "build", "-c", configPath, "-r", HERO], HERO, "browser build (rslib bundle, esm, externals)"));
  const entryPath = join(OUT, "browser/hero.esm.js");
  if (!existsSync(entryPath)) { writeJson(join(OUT, "build-record.json"), { results }); console.log("FAIL  no browser entry emitted"); process.exit(1); }
  let text = readFileSync(entryPath, "utf8");
  const before = sha(Buffer.from(text));
  // rewrite every bare import of a listed facade specifier (or one of its declared subpaths) to the facade URL
  const facadeFor = (spec) => {
    const exact = RULES.facades.find((f) => f.bareSpecifier === spec);
    if (exact) return { name: exact.name, spec };
    const parent = RULES.facades.find((f) => spec.startsWith(f.bareSpecifier + "/") && (f.requiredSubpaths || []).includes(spec.slice(f.bareSpecifier.length + 1)));
    if (parent) return { name: `${parent.name}-${spec.slice(parent.bareSpecifier.length + 1).replace(/\//g, "-")}`, spec };
    return null;
  };
  const facadeUrl = (name) => RULES.facadeUrlTemplate.replace("{hostProfileId}", HOST_PROFILE_ID).replace("{name}", name);
  const importGraph = [];
  const facadeMap = [];
  const unlisted = [];
  const importRe = /^(import\s+(?:[^'"]*?\s+from\s+)?)(["'])([^"']+)\2\s*;?$/gm;
  text = text.replace(importRe, (whole, head, q, spec) => {
    if (spec.startsWith(".") || spec.startsWith("/") || /^https?:/.test(spec)) { importGraph.push({ specifier: spec, resolvedTo: "bundled", importer: "browser/hero.esm.js" }); return whole; }
    const f = facadeFor(spec);
    if (!f) { unlisted.push(spec); return whole; }
    const url = facadeUrl(f.name);
    const facadeFile = join(STUDIO, "apps/studio/public", url);
    const facadeDigest = existsSync(facadeFile) ? sha(readFileSync(facadeFile)) : null;
    facadeMap.push({ bareSpecifier: spec, facadeUrl: url, ...(facadeDigest ? { facadeDigest } : {}) });
    importGraph.push({ specifier: spec, resolvedTo: "facade", facadeUrl: url, importer: "browser/hero.esm.js" });
    return `${head}${q}${url}${q};`;
  });
  // a runtime require or a second React copy would violate the facade rules
  const forbidden = ["require(", "react.production", "react-dom.production", "__SECRET_INTERNALS", "__CLIENT_INTERNALS"].filter((m) => text.includes(m));
  writeFileSync(entryPath, text);
  const entry = fileRecord(OUT, "browser/hero.esm.js");
  const styles = existsSync(join(OUT, "browser/hero.css")) ? [fileRecord(OUT, "browser/hero.css")] : [];
  const resources = readdirSync(join(OUT, "browser")).filter((f) => !/\.(js|css|map)$/.test(f)).map((f) => fileRecord(OUT, `browser/${f}`));
  const source = JSON.parse(readFileSync(join(OUT, "source-manifest.json"), "utf8"));
  const browser = {
    schemaVersion: 1,
    identity: source.identity,
    sourceDigest: source.sourceDigest,
    hostProfileId: HOST_PROFILE_ID,
    entry, resources, styles,
    facadeMapDigest: sha(Buffer.from(JSON.stringify(facadeMap))),
    facadeMap,
    importGraph,
  };
  writeJson(join(OUT, "browser-manifest.json"), browser);
  const npmStyles = fileRecord(HERO, "dist/styles.css");
  const record = {
    schemaVersion: 1,
    recordedAt: new Date().toISOString(),
    sourceDigest: source.sourceDigest,
    supportBuilds: supportBuilds.map(({ output, ...r }) => r),
    protectedCommands: results.map(({ output, ...r }) => r),
    npm: { tarball, files: npmFiles },
    browser: { entryBeforeRewrite: before, entry, styles, facadeMap, importGraph, unlistedBareImports: unlisted, forbiddenMarkers: forbidden, cssEqualsNpmStyles: styles[0]?.digest === npmStyles.digest },
    outputs: { manifest: "browser-manifest.json", entry: entry.path, styles: styles.map((s) => s.path) },
  };
  writeJson(join(OUT, "build-record.json"), record);
  writeJson(join(OUT, "build-log.json"), results);
  const failed = results.filter((r) => r.status !== "PASS").length + unlisted.length + forbidden.length;
  console.log(`\nnpm tarball ${tarball?.digest ?? "MISSING"}; browser entry ${entry.digest} (${entry.sizeBytes} B); css ${styles[0]?.digest ?? "MISSING"} equals npm dist/styles.css: ${record.browser.cssEqualsNpmStyles}`);
  if (unlisted.length) console.log(`FAIL  unlisted bare imports remain: ${unlisted.join(", ")} (FORBIDDEN_IMPORT)`);
  if (forbidden.length) console.log(`FAIL  forbidden markers in the bundle: ${forbidden.join(", ")}`);
  console.log(`${results.length + 2} build checks, ${failed} failures`);
  if (failed) process.exit(1);
}

function facades() {
  // Explicit named-export allowlists come from the host's real packages, enumerated in the Studio app's own
  // resolution context; public API names only (no internals), plus a default export for CJS-shaped packages.
  const dir = join(STUDIO, "apps/studio/public/host", HOST_PROFILE_ID, "facades");
  mkdirSync(dir, { recursive: true });
  const specs = [];
  const seen = new Set();
  const add = (s) => { if (!seen.has(s.spec)) { seen.add(s.spec); specs.push(s); } };
  for (const f of RULES.facades) {
    add({ name: f.name, spec: f.bareSpecifier, mode: f.exportsMode });
    for (const sub of f.requiredSubpaths || []) add({ name: `${f.name}-${sub.replace(/\//g, "-")}`, spec: `${f.bareSpecifier}/${sub}`, mode: "named" });
  }
  const probe = spawnSync(process.execPath, ["--input-type=module", "-e", `
    const out = {};
    for (const s of ${JSON.stringify(specs.map((s) => s.spec))}) {
      try { const m = await import(s); out[s] = { exports: Object.keys(m).filter((k) => k !== "module.exports" && k !== "default" && !k.startsWith("__")).sort(), hasDefault: "default" in m }; }
      catch (e) { out[s] = { error: String(e.message) }; }
    }
    console.log(JSON.stringify(out));`], { cwd: join(STUDIO, "apps/studio"), encoding: "utf8" });
  if (probe.status !== 0) { console.log("FAIL  host export enumeration: " + probe.stderr.slice(-400)); process.exit(1); }
  const enumerated = JSON.parse(probe.stdout.trim().split("\n").pop());
  const records = [];
  for (const s of specs) {
    const e = enumerated[s.spec];
    if (!e || e.error) { console.log(`FAIL  ${s.spec}: ${e?.error ?? "not enumerated"}`); process.exit(1); }
    const lines = [
      `// Host facade for ${JSON.stringify(s.spec)} bound to host profile ${HOST_PROFILE_ID} (DD-05 §6.2; generated by`,
      `// tools/component/hero-fixture.mjs facades). It exports the exact objects the bundled Studio client registered;`,
      `// it downloads nothing and exposes only this explicit allowlist. Importing it before the registry is`,
      `// bootstrapped fails closed.`,
      `const registry = globalThis.__anvilkitHostRegistry;`,
      `if (!registry || typeof registry.resolve !== "function") throw new Error("anvilkit host registry is not bootstrapped");`,
      `const m = registry.resolve(${JSON.stringify(HOST_PROFILE_ID)}, ${JSON.stringify(s.spec)});`,
      ...(e.hasDefault ? [`export default m.default;`] : []),
      ...e.exports.map((k) => `export const ${k} = m.${k};`),
      ``,
    ];
    const body = lines.join("\n");
    const file = join(dir, `${s.name}.js`);
    writeFileSync(file, body);
    records.push({ name: s.name, specifier: s.spec, exportsMode: s.mode, url: RULES.facadeUrlTemplate.replace("{hostProfileId}", HOST_PROFILE_ID).replace("{name}", s.name), digest: sha(Buffer.from(body)), exports: [...(e.hasDefault ? ["default"] : []), ...e.exports] });
    console.log(`facade ${s.name}: ${records.at(-1).exports.length} exports`);
  }
  writeJson(join(OUT, "facades.json"), { schemaVersion: 1, hostProfileId: HOST_PROFILE_ID, generatedAt: new Date().toISOString(), directory: relative(STUDIO, dir), facades: records });
}

function compare() {
  // S1-T01 acceptance: an independent rebuild (ANVILKIT_HERO_OUTPUT) against the durable record (ANVILKIT_HERO_RECORD).
  // Every identity below must agree byte for byte; a difference is printed and fails the command. The npm tarball
  // is compared file by file because its digest also covers files pnpm pack copies from the repository root.
  const RECORD = process.env.ANVILKIT_HERO_RECORD || join(REPO, "docs/architecture/audits/hero-fixture-2026-09-12");
  const load = (dir, f) => JSON.parse(readFileSync(join(dir, f), "utf8"));
  const rows = [];
  const check = (name, a, b) => rows.push({ name, recorded: a ?? "(absent)", rebuilt: b ?? "(absent)", same: a === b });
  const byPath = (list) => Object.fromEntries((list || []).map((f) => [f.path, f.digest ?? (f.missing ? "missing" : "?")]));
  const checkFiles = (name, a, b) => { for (const k of [...new Set([...Object.keys(a), ...Object.keys(b)])].sort()) check(`${name} ${k}`, a[k], b[k]); };
  const rs = load(RECORD, "source-manifest.json"), bs = load(OUT, "source-manifest.json");
  check("source baseRevision", rs.baseRevision, bs.baseRevision);
  check("sourceDigest", rs.sourceDigest, bs.sourceDigest);
  checkFiles("source", byPath(rs.files), byPath(bs.files));
  const ri = load(RECORD, "inventory.json"), bi = load(OUT, "inventory.json");
  check("component commit", ri.componentRepository.commit, bi.componentRepository.commit);
  check("studio commit", ri.studioRepository.commit, bi.studioRepository.commit);
  check("studio lockfile", ri.studioRepository.lockfile.digest, bi.studioRepository.lockfile.digest);
  check("studio workspace definition", ri.studioRepository.workspaceDefinition.digest, bi.studioRepository.workspaceDefinition.digest);
  for (const k of Object.keys(ri.toolchain)) check(`toolchain ${k}`, ri.toolchain[k], bi.toolchain[k]);
  checkFiles("repository file", byPath(ri.componentRepositoryFiles), byPath(bi.componentRepositoryFiles));
  checkFiles("template", byPath(ri.generatorTemplates), byPath(bi.generatorTemplates));
  checkFiles("protected test", byPath(ri.protectedTests), byPath(bi.protectedTests));
  checkFiles("patch", byPath(ri.patches.files), byPath(bi.patches.files));
  checkFiles("support", byPath(ri.supportClosure.files), byPath(bi.supportClosure.files));
  checkFiles("ui dist", byPath(ri.supportClosure.uiDist), byPath(bi.supportClosure.uiDist));
  checkFiles("analytics dist", byPath(ri.supportClosure.analyticsDist ?? []), byPath(bi.supportClosure.analyticsDist ?? []));
  const rb = load(RECORD, "build-record.json"), bb = load(OUT, "build-record.json");
  for (const r of rb.supportBuilds ?? []) check(`${r.label}`, r.status, (bb.supportBuilds ?? []).find((x) => x.label === r.label)?.status);
  for (const r of rb.protectedCommands) check(`protected ${r.label}`, r.status, bb.protectedCommands.find((x) => x.label === r.label)?.status);
  check("npm tarball", rb.npm.tarball?.digest, bb.npm.tarball?.digest);
  checkFiles("npm file", byPath(rb.npm.files), byPath(bb.npm.files));
  const npmDir = (dir) => { const d = join(dir, "npm"); const f = existsSync(d) && readdirSync(d).find((x) => x.endsWith(".tgz")); return f ? join(d, f) : null; };
  const rt = npmDir(RECORD) || npmDir(join(REPO, "outputs", `hero-fixture-v${heroManifest.version}`)), bt = npmDir(OUT);
  if (rt && bt) {
    const list = (t, tag) => { const d = join(OUT, "compare-tarball", tag); rmSync(d, { recursive: true, force: true }); mkdirSync(d, { recursive: true }); spawnSync("tar", ["-xzf", t, "-C", d]); return byPath(walk(d).map((rel) => fileRecord(d, rel))); };
    checkFiles("npm tarball member", list(rt, "recorded"), list(bt, "rebuilt"));
  } else rows.push({ name: "npm tarball members", recorded: rt ?? "(no tarball)", rebuilt: bt ?? "(no tarball)", same: false });
  check("browser entryBeforeRewrite", rb.browser.entryBeforeRewrite, bb.browser.entryBeforeRewrite);
  check("browser entry", rb.browser.entry.digest, bb.browser.entry.digest);
  checkFiles("browser style", byPath(rb.browser.styles), byPath(bb.browser.styles));
  check("cssEqualsNpmStyles", String(rb.browser.cssEqualsNpmStyles), String(bb.browser.cssEqualsNpmStyles));
  const rm = load(RECORD, "browser-manifest.json"), bm = load(OUT, "browser-manifest.json");
  check("browser manifest hostProfileId", rm.hostProfileId, bm.hostProfileId);
  check("browser manifest facadeMapDigest", rm.facadeMapDigest, bm.facadeMapDigest);
  check("browser manifest importGraph", JSON.stringify(rm.importGraph), JSON.stringify(bm.importGraph));
  check("browser manifest resources", JSON.stringify(rm.resources), JSON.stringify(bm.resources));
  const rf = load(RECORD, "facades.json"), bf = load(OUT, "facades.json");
  checkFiles("facade", Object.fromEntries(rf.facades.map((f) => [f.name, f.digest])), Object.fromEntries(bf.facades.map((f) => [f.name, f.digest])));
  const differing = rows.filter((r) => !r.same);
  for (const r of differing) console.log(`DIFF  ${r.name}\n      recorded ${r.recorded}\n      rebuilt  ${r.rebuilt}`);
  console.log(`${rows.length} identities compared, ${differing.length} differ`);
  writeJson(join(OUT, "compare.json"), { schemaVersion: 1, comparedAt: new Date().toISOString(), record: RECORD, rebuild: OUT, identities: rows.length, differing });
  if (differing.length) process.exit(1);
}

const cmd = process.argv[2] || "all";
if (!existsSync(HERO)) { console.error(`UNEXECUTED: ${HERO} is not a checkout`); process.exit(2); }
const supportBuilds = cmd === "support" || cmd === "all" ? support() : [];
if (cmd === "pin" || cmd === "all") pin();
if (cmd === "facades" || cmd === "all") facades();
if (cmd === "build" || cmd === "all") build(supportBuilds);
if (cmd === "compare") compare();
