#!/usr/bin/env python3
"""
生成 Chrome 扩展私钥并提取 manifest key 字段值。

**这是一次性本地工具，需要 cryptography 库（未列为运行时依赖）。**

Chrome 扩展使用 RSA 密钥对:
- .pem 文件: 私钥（保存到 ~/.browsertap/ 而非包数据目录）
- manifest.json 的 key 字段: 公钥 base64 编码（提交到仓库）

固定 key 后，扩展 ID 将保持不变，无论是 unpacked 还是打包模式。

依赖安装:
    pip install cryptography

私钥存储位置已更新为 ~/.browsertap/extension_private_key.pem，不随包分发。
"""
import base64
import sys
from pathlib import Path

try:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError:
    print("错误: 需要 cryptography 库")
    print("安装: pip install cryptography")
    sys.exit(1)


def generate_key_pair(output_pem: Path):
    """生成 RSA 密钥对并保存私钥"""
    # 生成 2048 位 RSA 私钥
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )

    # 保存私钥到 .pem 文件
    pem_data = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )
    output_pem.write_bytes(pem_data)
    output_pem.chmod(0o600)  # 只有所有者可读写

    # 提取公钥
    public_key = private_key.public_key()
    public_key_der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )

    # Base64 编码公钥 (manifest.json 的 key 字段)
    key_b64 = base64.b64encode(public_key_der).decode('ascii')

    return key_b64


def main():
    config_dir = Path.home() / ".browsertap"
    config_dir.mkdir(parents=True, exist_ok=True)
    pem_file = config_dir / "extension_private_key.pem"

    if pem_file.exists():
        print(f"错误: {pem_file} 已存在")
        print("如需重新生成，请先删除旧文件 (注意: 会改变扩展 ID)")
        sys.exit(1)

    print("生成扩展密钥...")
    manifest_key = generate_key_pair(pem_file)

    print(f"✓ 私钥已保存到: {pem_file}")
    print("✓ 文件权限: 600 (仅所有者可读)")
    print()
    print("=" * 70)
    print("请将以下内容添加到 manifest.json 的顶层:")
    print("=" * 70)
    print(f'  "key": "{manifest_key}"')
    print("=" * 70)
    print()
    print("注意:")
    print(f"1. 私钥保存在 {pem_file}")
    print("2. 私钥不随包分发（位于包数据目录外）")
    print("3. manifest.json 的 key 字段应该提交到仓库")
    print("4. 固定 key 后，扩展 ID 在所有环境保持一致")


if __name__ == "__main__":
    main()
