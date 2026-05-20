/**
 * Encrypt JSON for OPEN88 WPS API (uses site encrypt.js).
 * Usage: node open88_encrypt_bridge.js <rsa_hex_modulus> '<json_payload>'
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const rsaModulus = process.argv[2];
const payloadJson = process.argv[3] || "{}";

if (!rsaModulus) {
  console.error("usage: node open88_encrypt_bridge.js <rsa_hex> '<json>'");
  process.exit(1);
}

const encryptCode = fs.readFileSync(path.join(__dirname, "encrypt.js"), "utf8");
const sandbox = {
  console,
  window: {
    sessionStorage: { setItem() {}, getItem: () => null },
    localStorage: { getItem: (k) => (k === "lang" ? "CN" : null) },
    stop() {},
    location: { hostname: "www.open8808.com", origin: "https://www.open8808.com" },
  },
  document: { querySelector: () => null },
};
vm.createContext(sandbox);
vm.runInContext(encryptCode, sandbox);

const desKeyStr = sandbox.rndString();
const rsaOut = sandbox.rsaEncryptV2(
  rsaModulus,
  desKeyStr.split("").reverse().join("")
);
if (!rsaOut) {
  console.error(JSON.stringify({ error: "rsa_encrypt_failed" }));
  process.exit(2);
}

const desKey = sandbox.CryptoJS.enc.Utf8.parse(desKeyStr);
const desOut = sandbox.CryptoJS.DES.encrypt(payloadJson, desKey, {
  mode: sandbox.CryptoJS.mode.ECB,
  padding: sandbox.CryptoJS.pad.Pkcs7,
}).toString();

process.stdout.write(JSON.stringify({ RSA: rsaOut, DES: desOut }));
