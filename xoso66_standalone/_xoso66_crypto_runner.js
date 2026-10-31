
const fs = require('fs');
const path = require('path');
const implPath = path.join(__dirname, 'xoso66_crypto_impl.js');
if (!fs.existsSync(implPath)) {
  console.error(JSON.stringify({ ok: false, error: 'MISSING_CRYPTO_IMPL' }));
  process.exit(2);
}
const impl = require(implPath);
const [,, op, payload, cekK] = process.argv;
try {
  let out;
  if (op === 'encrypt') {
    const plain = JSON.parse(payload);
    out = impl.encrypt(plain, cekK || '');
  } else if (op === 'decrypt') {
    out = impl.decrypt(payload, cekK || '');
  } else {
    throw new Error('op must be encrypt|decrypt');
  }
  console.log(JSON.stringify({ ok: true, data: out }));
} catch (e) {
  console.error(JSON.stringify({ ok: false, error: String(e.message || e) }));
  process.exit(1);
}
