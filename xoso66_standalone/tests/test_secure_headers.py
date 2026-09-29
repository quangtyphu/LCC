# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import unittest

from xoso66_secure_headers import (
    generate_secure_headers,
    pack_v2_ciphertext,
    unpack_v2_ciphertext,
)


class TestSecureHeaders(unittest.TestCase):
    def test_matches_frontend_619(self):
        headers = generate_secure_headers(
            "https://w1hf52cb.whskxk5.com/server/user/userbanklist",
            (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "HeadlessChrome/140.0.7339.16 Safari/537.36"
            ),
            now_ms=1790582762053,
        )
        self.assertEqual(
            headers["c-a-i"],
            "762053cf9ea6f8f079260f6c760a8cce0671991790582",
        )
        self.assertEqual(
            headers["cf-pass"],
            "762711e3f67e4db08c2d78e3bf8aafe5edef919c8e34fb38e7e053",
        )
        self.assertEqual(
            headers["cf-auth-token"],
            (
                "Bearer.4e44b1a228796cb7128c3681aaddae3d359a52444a7ba57f."
                "193161239911.0df6f1e6"
            ),
        )
        self.assertEqual(
            headers["cf-con-s"],
            "94618604e63d04de50e4f497fd1428180dc1694eff014d10e0e3e0480563054c",
        )
        self.assertEqual(headers["cf-f-v"], "v2")

    def test_unpack_v2_ciphertext(self):
        cipher = "abc/def+ghi="
        signature = hashlib.md5(cipher.encode("utf-8")).hexdigest()
        packed = cipher[:3] + signature + cipher[3:]
        self.assertEqual(pack_v2_ciphertext(cipher), packed)
        self.assertEqual(unpack_v2_ciphertext(packed), cipher)

    def test_unpack_v2_rejects_bad_signature(self):
        with self.assertRaisesRegex(ValueError, "sai chữ ký"):
            unpack_v2_ciphertext("abc" + ("0" * 32) + "payload")


if __name__ == "__main__":
    unittest.main()
