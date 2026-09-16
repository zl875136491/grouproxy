import { access, chmod, cp, lstat, mkdir, readdir, rm } from "node:fs/promises";
import { basename, dirname, join, resolve } from "node:path";

const distDir = resolve(process.env.NEXT_DIST_DIR || ".next");
const standaloneDir = resolve(distDir, "standalone");
const standaloneDistDir = join(standaloneDir, basename(distDir));

async function copyDirectory(source, destination, { optional = false } = {}) {
  try {
    await access(source);
  } catch {
    if (optional) return;
    throw new Error(`Missing standalone build input: ${source}`);
  }
  await rm(destination, { recursive: true, force: true });
  await mkdir(dirname(destination), { recursive: true });
  await cp(source, destination, { recursive: true });
}

async function makeTreeReadable(root) {
  const directories = [root];
  while (directories.length > 0) {
    const directory = directories.pop();
    const directoryStat = await lstat(directory);
    await chmod(directory, directoryStat.mode | 0o555);

    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) {
        directories.push(path);
      } else if (entry.isFile()) {
        const fileStat = await lstat(path);
        const executable = fileStat.mode & 0o111 ? 0o111 : 0;
        await chmod(path, fileStat.mode | 0o444 | executable);
      }
    }
  }
}

await access(resolve(standaloneDir, "server.js"));
await copyDirectory(resolve(distDir, "static"), resolve(standaloneDistDir, "static"));
await copyDirectory(resolve("public"), resolve(standaloneDir, "public"), { optional: true });
await makeTreeReadable(standaloneDir);
