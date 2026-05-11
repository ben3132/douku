"""
signer.py - 抖音签名算法集成模块

集成两种签名算法：
  1. a_bogus - 用于视频详情、评论等接口
  2. x_bogus - 用于用户主页、搜索等接口

依赖：
  - gmssl (SM3 哈希，a_bogus 需要)
  - 标准库：hashlib, base64, time, random

用法：
  from modules.signer import Signer
  
  signer = Signer(user_agent="Mozilla/5.0...")
  
  # 获取 a_bogus
  a_bogus = signer.get_a_bogus(url_params_dict)
  
  # 获取 x_bogus
  params_with_xb, x_bogus = signer.get_x_bogus(url_path)

来源：
  - a_bogus: JoeanAmier/TikTokDownloader (GPLv3)
  - x_bogus: Evil0ctal/Douyin_TikTok_Download_API (Apache 2.0)
"""

import time
import base64
import hashlib
import random
import re
import sys
import io
from urllib.parse import urlencode, quote

# 尝试导入 gmssl，如果失败则提供提示
try:
    from gmssl import sm3, func
    HAS_GMSSL = True
except ImportError:
    HAS_GMSSL = False
    sm3 = None
    func = None


# ============================================================
# X-Bogus 签名算法
# ============================================================

class XBogus:
    """X-Bogus 签名生成器"""
    
    def __init__(self, user_agent: str = None) -> None:
        # fmt: off
        self.Array = [
            None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None,
            None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None,
            None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None,
            0, 1, 2, 3, 4, 5, 6, 7, 8, 9, None, None, None, None, None, None, None, None, None, None, None,
            None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None,
            None, None, None, None, None, None, None, None, None, None, None, None, 10, 11, 12, 13, 14, 15
        ]
        self.character = "Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe="
        # fmt: on
        self.ua_key = b"\x00\x01\x0c"
        self.user_agent = (
            user_agent
            if user_agent is not None and user_agent != ""
            else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )

    def md5_str_to_array(self, md5_str):
        if isinstance(md5_str, str) and len(md5_str) > 32:
            return [ord(char) for char in md5_str]
        else:
            array = []
            idx = 0
            while idx < len(md5_str):
                array.append(
                    (self.Array[ord(md5_str[idx])] << 4)
                    | self.Array[ord(md5_str[idx + 1])]
                )
                idx += 2
            return array

    def md5_encrypt(self, url_path):
        hashed_url_path = self.md5_str_to_array(
            self.md5(self.md5_str_to_array(self.md5(url_path)))
        )
        return hashed_url_path

    def md5(self, input_data):
        if isinstance(input_data, str):
            array = self.md5_str_to_array(input_data)
        elif isinstance(input_data, list):
            array = input_data
        else:
            raise ValueError("Invalid input type. Expected str or list.")

        md5_hash = hashlib.md5()
        md5_hash.update(bytes(array))
        return md5_hash.hexdigest()

    def encoding_conversion(self, a, b, c, e, d, t, f, r, n, o, i, _, x, u, s, l, v, h, p):
        y = [a]
        y.append(int(i))
        y.extend([b, _, c, x, e, u, d, s, t, l, f, v, r, h, n, p, o])
        re = bytes(y).decode("ISO-8859-1")
        return re

    def encoding_conversion2(self, a, b, c):
        return chr(a) + chr(b) + c

    def rc4_encrypt(self, key, data):
        S = list(range(256))
        j = 0
        encrypted_data = bytearray()

        for i in range(256):
            j = (j + S[i] + key[i % len(key)]) % 256
            S[i], S[j] = S[j], S[i]

        i = j = 0
        for byte in data:
            i = (i + 1) % 256
            j = (j + S[i]) % 256
            S[i], S[j] = S[j], S[i]
            encrypted_byte = byte ^ S[(S[i] + S[j]) % 256]
            encrypted_data.append(encrypted_byte)

        return encrypted_data

    def calculation(self, a1, a2, a3):
        x1 = (a1 & 255) << 16
        x2 = (a2 & 255) << 8
        x3 = x1 | x2 | a3
        return (
            self.character[(x3 & 16515072) >> 18]
            + self.character[(x3 & 258048) >> 12]
            + self.character[(x3 & 4032) >> 6]
            + self.character[x3 & 63]
        )

    def get_x_bogus(self, url_path):
        """生成 X-Bogus 签名，返回 (完整URL, x_bogus值, user_agent)"""
        array1 = self.md5_str_to_array(
            self.md5(
                base64.b64encode(
                    self.rc4_encrypt(self.ua_key, self.user_agent.encode("ISO-8859-1"))
                ).decode("ISO-8859-1")
            )
        )

        array2 = self.md5_str_to_array(
            self.md5(self.md5_str_to_array("d41d8cd98f00b204e9800998ecf8427e"))
        )
        url_path_array = self.md5_encrypt(url_path)

        timer = int(time.time())
        ct = 536919696
        array3 = []
        array4 = []
        xb_ = ""
        # fmt: off
        new_array = [
            64, 0.00390625, 1, 12,
            url_path_array[14], url_path_array[15], array2[14], array2[15], array1[14], array1[15],
            timer >> 24 & 255, timer >> 16 & 255, timer >> 8 & 255, timer & 255,
            ct >> 24 & 255, ct >> 16 & 255, ct >> 8 & 255, ct & 255
        ]
        # fmt: on
        xor_result = new_array[0]
        for i in range(1, len(new_array)):
            b = new_array[i]
            if isinstance(b, float):
                b = int(b)
            xor_result ^= b

        new_array.append(xor_result)

        idx = 0
        while idx < len(new_array):
            array3.append(new_array[idx])
            try:
                array4.append(new_array[idx + 1])
            except IndexError:
                pass
            idx += 2

        merge_array = array3 + array4

        garbled_code = self.encoding_conversion2(
            2,
            255,
            self.rc4_encrypt(
                "ÿ".encode("ISO-8859-1"),
                self.encoding_conversion(*merge_array).encode("ISO-8859-1"),
            ).decode("ISO-8859-1"),
        )

        idx = 0
        while idx < len(garbled_code):
            xb_ += self.calculation(
                ord(garbled_code[idx]),
                ord(garbled_code[idx + 1]),
                ord(garbled_code[idx + 2]),
            )
            idx += 3
        self.params = "%s&X-Bogus=%s" % (url_path, xb_)
        self.xb = xb_
        return (self.params, self.xb, self.user_agent)


# ============================================================
# A-Bogus 签名算法
# ============================================================

class ABogus:
    """A-Bogus 签名生成器（需要 gmssl 库支持 SM3）"""
    
    __filter = re.compile(r'%([0-9A-F]{2})')
    __arguments = [0, 1, 14]
    __ua_key = "\u0000\u0001\u000e"
    __end_string = "cus"
    __version = [1, 0, 1, 5]
    __browser = "1536|742|1536|864|0|0|0|0|1536|864|1536|864|1536|742|24|24|MacIntel"
    __reg = [
        1937774191,
        1226093241,
        388252375,
        3666478592,
        2842636476,
        372324522,
        3817729613,
        2969243214,
    ]
    __str = {
        "s0": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=",
        "s1": "Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe=",
        "s2": "Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe=",
        "s3": "DkdpghZmB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe=",
        "s4": "Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe",
    }

    def __init__(self, user_agent: str = None) -> None:
        self.size = 0
        self.chunk = []
        self.reg = self.__reg[:]
        self.ua_code = self.generate_ua_code(user_agent) if user_agent else [0] * 32
        self.browser_len = len(self.__browser)

    @staticmethod
    def rc4_encrypt(text: str, key: str) -> str:
        s = [i for i in range(256)]
        j = 0
        cipher = []
        for i in range(256):
            j = (j + s[i] + ord(key[i % len(key)])) % 256
            s[i], s[j] = s[j], s[i]
        i = j = 0
        for char in text:
            i = (i + 1) % 256
            j = (j + s[i]) % 256
            s[i], s[j] = s[j], s[i]
            cipher.append(chr(ord(char) ^ s[(s[i] + s[j]) % 256]))
        return ''.join(cipher)

    def sm3_to_array(self, data: list) -> list:
        """使用 SM3 哈希算法"""
        if not HAS_GMSSL:
            raise RuntimeError(
                "A-Bogus 签名需要 gmssl 库（SM3 哈希），SHA256 不能替代。\n"
                "请安装: pip install gmssl"
            )

        sm3_hash = sm3.sm3_hash(func.bytes_to_list(bytes(data)))
        return [int(sm3_hash[i:i+2], 16) for i in range(0, 64, 2)]

    def generate_ua_code(self, user_agent: str) -> list:
        """生成 UA 代码"""
        encrypted = self.rc4_encrypt(user_agent, self.__ua_key)
        b64_encoded = base64.b64encode(encrypted.encode('iso-8859-1')).decode('iso-8859-1')
        ua_code_raw = [ord(c) for c in b64_encoded]
        
        sm3_once = self.sm3_to_array(ua_code_raw)
        sm3_twice = self.sm3_to_array(sm3_once)
        return sm3_twice

    def generate_method_code(self, method: str = "GET") -> list:
        method_upper = method.upper()
        method_raw = [ord(c) for c in method_upper]
        return self.sm3_to_array(method_raw)

    def generate_params_code(self, url_params: str) -> list:
        if not url_params:
            return [0] * 32
        params_raw = [ord(c) for c in url_params]
        return self.sm3_to_array(params_raw)

    def generate_string_1(self, r1=None, r2=None, r3=None) -> str:
        r1 = r1 if r1 is not None else random.randint(0, 255)
        r2 = r2 if r2 is not None else random.randint(0, 255)
        r3 = r3 if r3 is not None else random.randint(0, 255)
        return chr(r1) + chr(r2) + chr(r3)

    def generate_string_2(self, url_params: str, method="GET", start_time=0, end_time=0) -> str:
        start_time = start_time or int(time.time() * 1000)
        end_time = end_time or (start_time + random.randint(4, 8))
        params_array = self.generate_params_code(url_params)
        method_array = self.generate_method_code(method)
        
        list4 = [
            (end_time >> 24) & 255,
            params_array[21],
            self.ua_code[23],
            (end_time >> 16) & 255,
            params_array[22],
            self.ua_code[24],
            (end_time >> 8) & 255,
            (end_time >> 0) & 255,
            (start_time >> 24) & 255,
            (start_time >> 16) & 255,
            (start_time >> 8) & 255,
            (start_time >> 0) & 255,
            method_array[21],
            method_array[22],
            int(end_time / 256 / 256 / 256 / 256) >> 0,
            int(start_time / 256 / 256 / 256 / 256) >> 0,
            self.browser_len,
        ]
        return self.list_to_string(list4)

    def list_to_string(self, arr: list) -> str:
        return ''.join(chr(b) for b in arr)

    def generate_result(self, string: str, table: str = "s4") -> str:
        result = []
        for char in string:
            idx = self.__str[table].index(char) if char in self.__str[table] else 0
            result.append(chr(idx))
        return base64.b64encode(''.join(result).encode('latin-1')).decode('latin-1')

    def get_value(self, url_params, method="GET") -> str:
        """生成 A-Bogus 签名"""
        if isinstance(url_params, dict):
            url_params = urlencode(url_params)
        
        string_1 = self.generate_string_1()
        string_2 = self.generate_string_2(url_params, method)
        string = string_1 + string_2
        return self.generate_result(string, "s4")


# ============================================================
# 统一签名器
# ============================================================

class Signer:
    """
    统一签名器
    
    用法：
        signer = Signer(user_agent="Mozilla/5.0...")
        
        # 生成 a_bogus
        a_bogus = signer.get_a_bogus({"aid": "6383", ...})
        
        # 生成 x_bogus
        url_with_xb, x_bogus = signer.get_x_bogus(url_path)
    """
    
    def __init__(self, user_agent: str = None):
        self.user_agent = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        self._a_bogus = None
        self._x_bogus = None
        self._init_signers()
    
    def _init_signers(self):
        """延迟初始化签名器"""
        self._x_bogus = XBogus(self.user_agent)
        # a_bogus 需要 gmssl
        if HAS_GMSSL:
            self._a_bogus = ABogus(self.user_agent)
        else:
            self._a_bogus = None
    
    def get_a_bogus(self, url_params) -> str:
        """
        生成 A-Bogus 签名
        
        参数：
            url_params: dict 或 str（URL 参数）
        
        返回：
            a_bogus 字符串
        """
        if self._a_bogus is None:
            raise RuntimeError("a_bogus 需要 gmssl 库支持，请运行: pip install gmssl")
        return self._a_bogus.get_value(url_params)
    
    def get_x_bogus(self, url_path: str) -> tuple:
        """
        生成 X-Bogus 签名
        
        参数：
            url_path: str（完整 URL 或参数部分）
        
        返回：
            (完整URL, x_bogus值, user_agent)
        """
        return self._x_bogus.get_x_bogus(url_path)
    
    def sign_url(self, url: str, method: str = "GET", sign_type: str = "both") -> dict:
        """
        签名 URL
        
        参数：
            url: 完整 URL
            method: GET 或 POST
            sign_type: "a" | "x" | "both"
        
        返回：
            {"url": 签名后URL, "a_bogus": ..., "x_bogus": ...}
        """
        result = {"url": url, "a_bogus": None, "x_bogus": None}
        
        if sign_type in ("a", "both") and HAS_GMSSL:
            a_bogus = self.get_a_bogus(url.split("?")[1] if "?" in url else "")
            result["a_bogus"] = a_bogus
            result["url"] = f"{result['url']}&a_bogus={quote(a_bogus, safe='')}"
        
        if sign_type in ("x", "both"):
            url_with_xb, x_bogus, _ = self.get_x_bogus(result["url"])
            result["x_bogus"] = x_bogus
            result["url"] = url_with_xb
        
        return result


# ============================================================
# 便捷函数
# ============================================================

_default_signer = None

def get_signer(user_agent: str = None) -> Signer:
    """获取全局签名器实例"""
    global _default_signer
    if _default_signer is None or user_agent:
        _default_signer = Signer(user_agent)
    return _default_signer


def sign_request(url: str, user_agent: str = None, sign_type: str = "x") -> str:
    """
    快捷签名函数
    
    参数：
        url: 待签名 URL
        user_agent: User-Agent
        sign_type: "a" | "x" | "both"
    
    返回：
        签名后的完整 URL
    """
    signer = get_signer(user_agent)
    result = signer.sign_url(url, sign_type=sign_type)
    return result["url"]


# ============================================================
# 命令行测试
# ============================================================

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="抖音签名工具")
    parser.add_argument("--url", help="待签名的 URL")
    parser.add_argument("--type", choices=["a", "x", "both"], default="x", help="签名类型")
    parser.add_argument("--check", action="store_true", help="检查依赖")
    
    args = parser.parse_args()
    
    if args.check:
        print("依赖检查：")
        print(f"  gmssl: {'✅ 已安装' if HAS_GMSSL else '❌ 未安装 (a_bogus 不可用)'}")
        print(f"  hashlib: ✅ 内置")
        print(f"  base64: ✅ 内置")
        if not HAS_GMSSL:
            print()
            print("💡 安装 gmssl 以启用 a_bogus 签名：")
            print("   pip install gmssl")
    
    if args.url:
        print(f"\n原始 URL: {args.url[:80]}...")
        
        if args.type in ("a", "both") and HAS_GMSSL:
            signer = Signer()
            a_bogus = signer.get_a_bogus(args.url.split("?")[1] if "?" in args.url else "")
            print(f"\na_bogus: {a_bogus}")
        
        if args.type in ("x", "both"):
            signer = Signer()
            url_with_xb, x_bogus, ua = signer.get_x_bogus(args.url)
            print(f"\nx_bogus: {x_bogus}")
            print(f"\n签名后 URL: {url_with_xb[:100]}...")
