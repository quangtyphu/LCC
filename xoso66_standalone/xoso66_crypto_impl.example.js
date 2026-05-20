// Đổi tên thành xoso66_crypto_impl.js và điền code thật từ web XOSO66
module.exports = {
  encrypt(plain, cekK) {
    // plain: object { amount, payType, channelId, ... }
    // return: string body gửi POST depositorder
    throw new Error('Chưa implement — copy từ index.*.js');
  },
  decrypt(cipher, cekK) {
    // cipher: response string từ server
    // return: object hoặc JSON string
    throw new Error('Chưa implement');
  },
  // generateCekK() { ... }  // optional
};
