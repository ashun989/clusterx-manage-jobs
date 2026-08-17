import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { build } from "esbuild";

const root = resolve(import.meta.dirname);
const output = resolve(root, "dist");

await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });

await Promise.all([
  build({
    entryPoints: [resolve(root, "src/popup.ts")],
    bundle: true,
    outfile: resolve(output, "popup.js"),
    format: "iife",
    platform: "browser",
    target: "chrome114",
    sourcemap: false,
    minify: false,
  }),
  build({
    entryPoints: [resolve(root, "src/content.ts")],
    bundle: true,
    outfile: resolve(output, "content.js"),
    format: "iife",
    platform: "browser",
    target: "chrome114",
    sourcemap: false,
    minify: false,
  }),
]);

await Promise.all([
  cp(resolve(root, "src/popup.html"), resolve(output, "popup.html")),
  cp(resolve(root, "src/popup.css"), resolve(output, "popup.css")),
  cp(resolve(root, "manifest.json"), resolve(output, "manifest.json")),
]);

const manifest = JSON.parse(await readFile(resolve(output, "manifest.json"), "utf8"));
if (manifest.manifest_version !== 3 || manifest.permissions.includes("storage")) {
  throw new Error("Generated manifest violates the extension security boundary");
}

await writeFile(
  resolve(output, "BUILD_INFO.txt"),
  "Built from chrome-extension. Load this directory as an unpacked extension.\n",
  "utf8",
);
