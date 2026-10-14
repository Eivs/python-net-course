#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_18_https_tls.py -- 第 18 课：HTTPS，加密到底加在哪一层
===========================================================

【一句话原理】
    HTTPS = HTTP + TLS。
    HTTP 报文一个字都没改, 只是被 TLS 整个包起来, 在 TCP 之上多了一层
    「握手 + 加密 + 完整性校验」。所以 TCP 之上依次是:

        应用层  HTTP / SMTP / DNS-over-TLS     <- 明文内容在这里
        ─────────────────────────────────
        安全层  TLS（记录协议 + 握手协议）
        ─────────────────────────────────
        传输层  TCP（443 端口; 也可以用 QUIC/UDP 跑 HTTP/3）
        ─────────────────────────────────
        网络层  IP

【TLS 握手做了什么（1.2 版简化）】
    1. ClientHello   客户端: 我支持的 TLS 版本、密码套件列表、一个随机数、
                              以及 SNI（我要访问的域名, 明文!）
    2. ServerHello   服务端: 选定版本和套件、自己的随机数
    3. Certificate   服务端: 把证书链发过来（自己的证书 + 中间 CA 证书）
    4. 验证          客户端: 用系统内置的根 CA 逐级验签, 校验有效期、
                              域名是否匹配、是否被吊销 -> 都通过才继续
    5. 密钥交换      双方算出一个只有彼此知道的「会话密钥」(
                              1.2 用 RSA/ECDHE, 1.3 只剩 (EC)DHE 前向安全方案)
    6. Finished      双方各发一个用会话密钥加密/MAC 的校验值, 确认握手没被篡改
    7. 之后所有 HTTP 数据都用会话密钥做对称加密（AES-GCM 之类）

    为什么用非对称只做握手、之后换对称: 非对称加解密慢几个数量级,
    而对称密钥必须安全地交换, 所以「用非对称换对称密钥」是标准套路。

【本课路线】
    1. 连一个真实网站, 打印 TLS 版本 / 密码套件 / 证书 / 证书链
    2. SNI 实验: 同一个 IP, 带不带域名差别巨大
    3. 本地自签证书实验: 亲手证明「HTTPS 的安全性来自证书链和主机名校验」
       a. 默认校验 -> 失败（自签 CA 不被信任）
       b. 把自签证书当 CA -> 成功
       c. 关掉校验 -> 也成功, 但中间人可以随便解密（为什么不能这么写代码）
       d. 域名不匹配 -> 失败
    4. ALPN: 一条 TLS 连接怎么协商出 h2 还是 http/1.1
    5. 真实 HTTPS 请求: http.client 的 HTTPSConnection
    6. 生产清单: 证书管理、HSTS、mTLS、TLS 1.3、常见坑

运行：python3 demo_18_https_tls.py
"""

from __future__ import annotations

import datetime
import http.client
import os
import socket
import ssl
import subprocess
import sys
import threading

CERT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tls_demo")


def title(text: str) -> None:
    print()
    print("=" * 74)
    print(text)
    print("=" * 74)


def days_until(when: str | None) -> str:
    """证书里的时间格式形如 'Oct 14 06:00:00 2026 GMT', 算一下还剩多少天。"""
    if not when:
        return "?"
    try:
        expires = datetime.datetime.strptime(when, "%b %d %H:%M:%S %Y %Z")
    except ValueError:
        return "?"
    delta = expires - datetime.datetime.utcnow()
    return f"（还剩 {delta.days} 天）" if delta.days >= 0 else f"（已过期 {-delta.days} 天！）"


def cert_summary(der: bytes) -> str:
    """用 openssl 命令行解析一张 DER 证书的 subject/issuer（顺便演示排障工具）。"""
    import tempfile
    pem = ssl.DER_cert_to_PEM_cert(der)
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as fh:
        fh.write(pem)
        path = fh.name
    try:
        out = subprocess.run(["openssl", "x509", "-in", path, "-noout", "-subject", "-issuer"],
                             capture_output=True, text=True, errors="replace", timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return "(无法用 openssl 解析)"
    finally:
        os.unlink(path)
    parts = [line.split("=", 1)[1].strip() for line in out.strip().splitlines() if "=" in line]
    return " | ".join(parts) if parts else out.strip()


def describe_cert(cert: dict | None, indent: str = "    ") -> None:
    """把 ssl 模块解析出来的证书字典打印成人能读的样子。"""
    if not cert:
        print(f"{indent}(没有证书)")
        return
    subject = {k: v for item in cert.get("subject", ()) for k, v in item}
    issuer = {k: v for item in cert.get("issuer", ()) for k, v in item}
    print(f"{indent}主题 (subject)  : {subject.get('commonName', '?')}  "
          f"[{', '.join(f'{k}={v}' for k, v in subject.items())}]")
    print(f"{indent}签发者 (issuer) : {issuer.get('organizationName', issuer.get('commonName', '?'))}")
    print(f"{indent}有效期          : {cert.get('notBefore')}  ->  {cert.get('notAfter')} "
          f"{days_until(cert.get('notAfter'))}")
    print(f"{indent}序列号          : {cert.get('serialNumber')}")
    print(f"{indent}版本            : v{cert.get('version')}")
    if cert.get("signatureAlgorithm"):
        print(f"{indent}签名算法        : {cert.get('signatureAlgorithm')}")
    sans = [v for k, v in cert.get("subjectAltName", ())]
    print(f"{indent}SAN（这证书对哪些域名有效）: {sans[:6]}{' ...' if len(sans) > 6 else ''}")
    if cert.get("bits"):
        print(f"{indent}密钥长度        : {cert.get('bits')} 位")


# ================================================================ 自签证书
def ensure_self_signed_cert() -> tuple[str, str] | None:
    """用 openssl 生成一张自签证书（含 SAN=localhost/127.0.0.1）。"""
    os.makedirs(CERT_DIR, exist_ok=True)
    cert_path = os.path.join(CERT_DIR, "cert.pem")
    key_path = os.path.join(CERT_DIR, "key.pem")
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", key_path, "-out", cert_path, "-days", "2",
        "-subj", "/CN=localhost/O=python-net-course",
        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  openssl 执行失败: {exc}")
        return None
    if result.returncode != 0:
        print(f"  生成证书失败: {result.stderr.strip()[:200]}")
        return None
    print(f"  已生成自签证书: {cert_path}")
    return cert_path, key_path


class TinyHttpsServer:
    """一个最小的 HTTPS 服务器: TLS 包裹 socket, 然后在里面跑 HTTP。"""

    def __init__(self, cert_path: str, key_path: str, alpn: list[str] | None = None):
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert_path, key_path)
        if alpn:
            self.context.set_alpn_protocols(alpn)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.address = self.sock.getsockname()
        self._stop = threading.Event()
        self.sni_seen: list[str] = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        self.sock.settimeout(0.3)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            # 这一步就是「TLS 握手」: wrap_socket 内部完成 ClientHello -> Finished 的全过程
            tls = self.context.wrap_socket(conn, server_side=True)
        except (ssl.SSLError, OSError):
            return
        with tls:
            try:
                request = tls.recv(4096)
                body = b"hello from a self-signed HTTPS server\n"
                tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                            + body)
                _ = request
            except (ssl.SSLError, OSError):
                pass

    def close(self) -> None:
        self._stop.set()
        self.sock.close()


def main() -> None:
    # ---------------------------------------------------------------- 第 1 步
    title("第 1 步：连一个真实网站，看看 TLS 握手的结果")

    context = ssl.create_default_context()          # 用系统信任的根 CA, 并开启主机名校验
    print(f"  默认上下文: 校验模式={context.verify_mode}, 检查主机名={context.check_hostname}")
    print(f"  底层 OpenSSL: {ssl.OPENSSL_VERSION}")

    try:
        with socket.create_connection(("example.com", 443), timeout=8) as raw:
            # server_hostname 就是 SNI: 握手时明文告诉服务器"我要访问哪个域名"
            with context.wrap_socket(raw, server_hostname="example.com") as tls:
                print(f"\n  握手成功:")
                print(f"    TLS 版本      : {tls.version()}")
                print(f"    密码套件      : {tls.cipher()[0]}")
                print(f"    套件协议版本  : {tls.cipher()[1]},  密钥长度 {tls.cipher()[2]} 位")
                print(f"    会话是否复用  : {tls.session_reused}")
                print(f"    ALPN 协商结果 : {tls.selected_alpn_protocol()}")
                print("\n  服务端证书:")
                describe_cert(tls.getpeercert(), indent="    ")
                try:
                    chain = tls.get_verified_chain()        # Python 3.13+: 已校验的完整链
                    print(f"\n  证书链（共 {len(chain)} 张, 从叶证书到根 CA）:")
                    for i, der in enumerate(chain, 1):
                        print(f"    第 {i} 张 ({len(der)} 字节 DER): {cert_summary(der)}")
                    print("""    为什么要看链: 服务器只发「自己的证书 + 中间 CA」, 根 CA 在客户端本机。
    如果服务器漏发中间证书, 新浏览器可能能容忍（会自动补）, 但很多老客户端/
    其他语言 SDK 会直接报 unable to get local issuer certificate。""")
                except AttributeError:
                    print("\n  （当前 Python 没有 get_verified_chain, 跳过证书链展示）")
    except OSError as exc:
        print(f"  连接 example.com:443 失败（可能没网）: {exc}")

    print("""
  读一遍上面的输出, 你已经看到了 TLS 的全部「可见结果」:
    * TLS 1.3 只有 1 个 RTT（1.2 要 2 个）, 所以 HTTPS 首包延迟比你想的低
    * 密码套件形如 TLS_AES_128_GCM_SHA256: 密钥交换 + 对称加密 + 摘要算法的组合
    * 证书里最重要的是 SAN（Subject Alternative Name）: 现代浏览器不看 CN 只看 SAN
    * 有效期一般 90 天（Let's Encrypt 推动的短周期）, 所以自动化续期是刚需
""")

    # ---------------------------------------------------------------- 第 2 步
    title("第 2 步：SNI 实验 —— 同一个 IP，域名决定你拿到哪张证书")

    try:
        ip = socket.gethostbyname("example.com")
        print(f"  example.com 解析到 {ip}（同一台服务器上显然不只托管一个网站）")

        # (a) 不带 SNI: 关掉校验并省略 server_hostname
        lax = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        lax.check_hostname = False
        lax.verify_mode = ssl.CERT_NONE
        print("\n  (a) 不发送 SNI（直接连 IP, 不带 server_hostname）:")
        try:
            with socket.create_connection((ip, 443), timeout=8) as raw:
                with lax.wrap_socket(raw) as tls:
                    der = tls.getpeercert(binary_form=True)
                    print(f"      握手完成, 拿到一张 {len(der) if der else 0} 字节的证书")
                    print(f"      但这是服务端自己挑的默认证书, 不一定是你要的那个站点")
        except ssl.SSLError as exc:
            print(f"      握手直接被拒: {exc}")
            print("""      <- 这是很多 CDN 的做法: 没有 SNI 就不知道怎么选证书, 干脆拒掉。
         所以「用 IP 直连 + 自己填 Host 头」这个 HTTP 时代的技巧,
         在 HTTPS 下根本行不通 —— 握手阶段就必须提供域名。""")

        # (b) 带 SNI
        print("\n  (b) 发送 SNI=example.com:")
        try:
            with socket.create_connection((ip, 443), timeout=8) as raw:
                with context.wrap_socket(raw, server_hostname="example.com") as tls:
                    cert = tls.getpeercert()
                    sans = [v for k, v in cert.get("subjectAltName", ())]
                    print(f"      拿到正确证书, SAN 包含 example.com: {any('example.com' in s for s in sans)}")
                    print(f"      SAN 列表: {sans}")
        except (ssl.SSLError, OSError) as exc:
            print(f"      失败: {exc}")
    except OSError as exc:
        print(f"  实验失败（网络问题）: {exc}")

    print("""
  SNI 的意义: 一台服务器（一个 IP）上托管几万个网站, 握手时服务端还不知道你要哪个域名,
  没法决定发哪张证书。所以客户端必须在 ClientHello 里明文告诉它 —— 这就是 SNI。
  代价: 中间人能看到你访问的域名（虽然看不到路径和内容）。
  对策: ECH（Encrypted Client Hello）, 把 SNI 也加密, 已在部分浏览器/厂商落地。

  另一个坑: 有些老服务器/防火墙遇到「不认识的 SNI」会直接断连,
  所以用 IP 直连 + 正确 Host 头的老办法在 HTTPS 下可能直接握手失败。
""")

    # ---------------------------------------------------------------- 第 3 步
    title("第 3 步：自签证书实验 —— 亲手证明 HTTPS 靠什么保证安全")

    generated = ensure_self_signed_cert()
    if not generated:
        print("  无法生成自签证书（需要 openssl）, 跳过本步。")
    else:
        cert_path, key_path = generated
        server = TinyHttpsServer(cert_path, key_path, alpn=["http/1.1"])
        host, port = server.address
        print(f"  本地 HTTPS 服务器: https://{host}:{port}（用自签证书）")

        # (a) 默认校验 -> 必然失败
        print("\n  (a) 客户端用系统信任链校验 -> 应该失败:")
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=5) as raw:
                with ctx.wrap_socket(raw, server_hostname="localhost"):
                    print("      居然成功了? 说明系统信任了这张自签证书（不太可能）")
        except ssl.SSLCertVerificationError as exc:
            print(f"      SSLCertVerificationError: {exc.verify_message if hasattr(exc, 'verify_message') else exc}")
            print("      <- 这就是浏览器里那句「您的连接不是私密连接」的技术真相:")
            print("         证书是自签的, 不在系统信任的根 CA 列表里, 无法建立信任链。")

        # (b) 把自签证书当作 CA 信任 -> 成功
        print("\n  (b) 把这张自签证书加入信任列表（相当于手动「信任」它）-> 成功:")
        try:
            ctx = ssl.create_default_context(cafile=cert_path)
            ctx.set_alpn_protocols(["http/1.1"])   # 客户端也要声明支持哪些应用层协议
            with socket.create_connection((host, port), timeout=5) as raw:
                with ctx.wrap_socket(raw, server_hostname="localhost") as tls:
                    print(f"      握手成功: {tls.version()}, 套件 {tls.cipher()[0]}")
                    print(f"      ALPN 协商结果: {tls.selected_alpn_protocol()}")
                    cert = tls.getpeercert()
                    print(f"      证书主题: {dict(x for item in cert['subject'] for x in item)}")
                    tls.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                    data = b""
                    while True:
                        chunk = tls.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    print(f"      加密通道里的 HTTP 响应: {data.decode().strip().splitlines()[0]}")
                    print(f"      解密后的 body: {data.decode().split(chr(13)+chr(10)+chr(13)+chr(10), 1)[1].strip()!r}")
        except ssl.SSLError as exc:
            print(f"      失败: {exc}")

        # (c) 关掉校验 -> 也能成功, 但危险
        print("\n  (c) 关闭证书校验（verify_mode=CERT_NONE）-> 也成功, 但这是不安全的:")
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with socket.create_connection((host, port), timeout=5) as raw:
                with ctx.wrap_socket(raw, server_hostname="localhost") as tls:
                    print(f"      握手「成功」, 但证书完全没验证 -> 中间人换一张自己的证书你也发现不了")
                    print(f"      getpeercert() 返回空: {tls.getpeercert()!r}")
                    print("""      <- 这就是「加密但不可信」: 数据确实被加密了, 可是你加密给了一个冒充者。
         所有 API 客户端里出现的 verify=False / rejectUnauthorized: false /
         curl -k 都属于这一类, 只能用于测试环境。""")
        except ssl.SSLError as exc:
            print(f"      失败: {exc}")

        # (d) 域名不匹配 -> 失败
        print("\n  (d) 证书有效但域名不匹配 -> 失败（主机名校验独立于 CA 校验）:")
        try:
            ctx = ssl.create_default_context(cafile=cert_path)
            with socket.create_connection((host, port), timeout=5) as raw:
                with ctx.wrap_socket(raw, server_hostname="evil.example.com"):
                    print("      居然成功了?（不应该）")
        except ssl.SSLCertVerificationError as exc:
            print(f"      SSLCertVerificationError: {exc}")
            print("      <- 证书里 SAN 只写了 localhost/127.0.0.1, 所以访问别的域名必须失败。")
            print("         这条校验挡住的是「攻击者拿自己的合法证书来冒充你的站点」。")

        server.close()

    # ---------------------------------------------------------------- 第 4 步
    title("第 4 步：ALPN —— TLS 里顺手把 HTTP 版本谈好")

    print("""  ALPN（Application-Layer Protocol Negotiation）让客户端在 ClientHello 里列出
  自己支持的协议（如 ["h2", "http/1.1"]）, 服务端挑一个放回 ServerHello。
  这样浏览器第一次握手后就知道该说 HTTP/2 还是 HTTP/1.1, 不用多花一个来回。

  上面的本地服务器声明只支持 http/1.1, 所以协商结果就是 http/1.1。
  真实站点大多会协商成 h2; 支持 HTTP/3 的站点还会通过 Alt-Svc 头告诉浏览器
  「下次可以用 h3（在 UDP 443 上）直接找我」—— 这就是 HTTP/3 的普及路径。
""")

    # ---------------------------------------------------------------- 第 5 步
    title("第 5 步：真实 HTTPS 请求（HTTPS = TLS 隧道里的 HTTP）")

    try:
        ctx = ssl.create_default_context()
        # http.client 的 HTTPSConnection 就是「TCP + TLS + HTTP」的组合封装
        conn = http.client.HTTPSConnection("example.com", 443, timeout=10, context=ctx)
        conn.connect()                          # 先只做 TCP + TLS 握手, 还没发 HTTP
        tls_version = conn.sock.version() if conn.sock else "(没有 TLS 连接)"
        conn.request("GET", "/", headers={"User-Agent": "demo_18/1.0", "Connection": "close"})
        resp = conn.getresponse()
        body = resp.read()
        print(f"  HTTPS GET https://example.com/ -> {resp.status} {resp.reason}")
        print(f"  响应头: Content-Type={resp.getheader('Content-Type')}, "
              f"Content-Length={resp.getheader('Content-Length')}, "
              f"Transfer-Encoding={resp.getheader('Transfer-Encoding')}")
        if resp.getheader("Transfer-Encoding") == "chunked":
            print("    注意: 这条响应用的是 chunked（第 15 课）而不是 Content-Length,")
            print("          所以头里没有长度 —— http.client 已经替你把分块解好了。")
        print(f"  body 前 80 字节: {body[:80]!r}")
        print(f"  底层 TLS 版本: {tls_version}（socket 被替换成了 SSLSocket）")
        conn.close()
    except OSError as exc:
        print(f"  真实 HTTPS 请求失败（网络问题）: {exc}")

    # ---------------------------------------------------------------- 第 6 步
    title("第 6 步：生产实践清单")

    print("""  【证书】
    □ 用 ACME（Let's Encrypt）自动签发与续期, 别手工上传（90 天过期是事故高发点）
    □ 监控到期时间（至少提前 21 天告警）, 顺便监控证书链是否完整
    □ 证书链要发全（服务器要发中间 CA, 否则老客户端会验不过）

  【协议与套件】
    □ 只允许 TLS 1.2+, 优先 1.3（更少的往返、去掉了一堆历史包袱套件）
    □ 现代套件基本都是 ECDHE + AEAD（AES-GCM / ChaCha20-Poly1305）, 前向安全
    □ 关掉压缩（CRIME）、关掉重协商、关掉 RC4/3DES 之类老古董

  【应用层】
    □ 开 HSTS（Strict-Transport-Security）: 强制后续都用 HTTPS, 防降级中间人
    □ Cookie 加 Secure + HttpOnly + SameSite
    □ 别用证书固定（pinning）除非你能接受客户端发版跟不上证书轮换的现实
    □ 内网服务间也要 TLS（mTLS 双向认证）, 「内网安全」是过时假设

  【排障顺序】（遇到 SSL 错误时）
    1) openssl s_client -connect host:443 -servername host   看握手细节和证书链
    2) 报 hostname mismatch -> 检查 SAN 和访问的域名（IP 直连最常见）
    3) 报 unable to get local issuer -> 服务器没发中间证书, 或客户端 CA 库太旧
    4) 报 certificate expired -> 过期, 完
    5) 只有某个客户端报错 -> 多半是它不支持你的 TLS 版本/套件（老 Android、老 Java）
""")

    title("本课小结")
    print("""1. HTTPS = HTTP 跑在 TLS 之上; 协议没变, 只是多了一层隧道。
2. TLS 握手: 协商版本/套件 -> 传证书 -> 验签+验域名+验有效期 -> 换对称密钥 -> Finished。
3. HTTPS 的安全不是来自「加密」本身, 而是来自「证书链 + 主机名校验」建立的信任。
   自签证书实验 (a)(b)(c)(d) 四种结果就是这个结论的直接证据。
4. SNI 让一个 IP 服务无数域名, 代价是域名在握手中明文可见（ECH 正在解决）。
5. ALPN 在握手里顺便谈好 HTTP 版本; HTTP/3 则换到 UDP 上（QUIC）。
6. 下一章: 把前面 18 课合起来, 写一个完整的服务与客户端, 并对比并发模型的性能。
""")
    print("下一课: 06_project/mini_http_server.py")


if __name__ == "__main__":
    main()
