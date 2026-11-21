// Local acceptance peer. Uses the installed Link's Linux executor and frame filter.
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const [agent, workspace] = process.argv.slice(2).map((v) => resolve(v));
const load = (name) =>
  import(pathToFileURL(join(agent, "dist", name + ".js")).href);
const { ExecServer } = await load("execServer");
const { decide } = await load("filter");
const root = join(workspace, "reports");
await mkdir(root);
const home = await mkdtemp(join(workspace, "codex-"));
const executor = new ExecServer({
  codexBin: join(agent, "node_modules", ".bin", "codex"),
  bwrap: "/usr/bin/bwrap",
  outerSandbox: true,
  roots: [root],
  codexHome: home,
  port: 0,
});
let socket;
try {
  await executor.start();
  if (!executor.sandboxed)
    throw new Error("Expected a sandboxed local executor");
  socket = new WebSocket(executor.url);
  await new Promise((resolve, reject) => {
    socket.onopen = resolve;
    socket.onerror = reject;
  });
  let id = 0;
  async function call(method, params) {
    const frame = { id: ++id, method, params };
    if (
      !decide(frame, { sandbox: "workspace-write", network: false }, [root])
        .allow
    )
      throw new Error("Link declined the report operation");
    const answer = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        socket.removeEventListener("message", receive);
        reject(new Error("Link response timed out"));
      }, 5000);
      function receive(e) {
        const reply = JSON.parse(e.data);
        if (reply.id !== frame.id) return;
        clearTimeout(timer);
        socket.removeEventListener("message", receive);
        if (reply.error) reject(new Error("Local executor refused " + method));
        else resolve(reply.result);
      }
      socket.addEventListener("message", receive);
    });
    socket.send(JSON.stringify(frame));
    return answer;
  }
  await call("initialize", { clientName: "registry-acceptance" });
  socket.send(JSON.stringify({ method: "initialized", params: {} }));
  const path = pathToFileURL(join(root, "report.txt")).href;
  await call("fs/writeFile", {
    path,
    dataBase64: Buffer.from("Synthetic monthly report").toString("base64"),
    sandbox: null,
  });
  const content = await call("fs/readFile", { path, sandbox: null });
  process.stdout.write(
    JSON.stringify({
      body: Buffer.from(content.dataBase64, "base64").toString("utf8"),
    }) + "\n",
  );
} finally {
  socket?.close();
  await executor.stop();
  await rm(home, { recursive: true, force: true });
}
