# pow_hashdogos

Local GPU mining for **HashDogos** ([hashdogos.fun](https://hashdogos.fun), Robinhood Chain) — an NFT with **keccak256 target-based proof-of-work** on chain. Your machine finds a nonce that meets the difficulty → submit `mine(nonce, anchorBlock)`, a **paid mint** (each one = on-chain `mintPrice`, which rises with global supply).

- `hashdogos/hashdogos.py` — self-contained miner (OpenCL keccak256 + first-launch kernel self-test + pre-submit eth_call simulation + price lock + spend cap + submission). Single file; change the config at the top to repurpose for another project.
- `hashdogos/SKILL.md` — a [CC] skill so your own Claude can build a miner like this from scratch.

## How it works

```
hash = keccak256( abiEncode(
         address contract, uint256 chainId, address miner,
         uint256 nonce, bytes32 previousWork, bytes32 anchor) )     # 6×32 = 192 bytes
valid  <=>  hash < currentTarget(miner)        # integer comparison, per-miner difficulty
submit  mine(uint256 nonce, uint256 anchorBlock)  value = mintPrice
```

`previousWork` changes every time someone mints (the miner re-reads it each round); `anchor` has a freshness window — expired submissions revert. **This is a lottery, not a finite search**: a valid nonce can appear early with luck; the only way to be faster is more hashrate.

> Unlike SHA-256 leading-zero schemes (e.g. HashBroker): this one is **keccak256** + **integer target comparison** (not counting leading zeros) + **per-miner difficulty** + **standard abiEncode** (each field padded to 32 bytes). Don't mix them up.

## Usage

```bash
pip install pyopencl eth-account numpy eth-hash[pycryptodome]

# use a small burner private key ONLY!
export PK=0xyourburnerkey        # Windows PowerShell: $env:PK="0x..."

# 1) dry run first (no tx sent; verifies kernel self-test + simulation passes):
COUNT=1 python hashdogos/hashdogos.py

# 2) real run (spends real money, ~mintPrice ETH each):
COUNT=10 PRICE_LOCK=480000000000000 CAP_ETH=0.006 DO=1 python hashdogos/hashdogos.py
```

Environment variables:
- `COUNT` — how many to mine
- `PRICE_LOCK` — price lock (wei). If the price changes, stop — never quietly pay the new higher price. `0` = no lock.
- `CAP_ETH` — total spend cap; stops when exceeded.
- `DO=1` — **transactions are only sent when this is set**; without it = mine + simulate only, zero spending.

Measured on an RTX 5090: ≈ 1.2 GH/s keccak256 per card (slower than SHA-256 because each hash needs 2 keccak permutations). **Multi-GPU is supported**: every OpenCL device found is used automatically — one thread per GPU with disjoint nonce ranges, so N cards ≈ N× the hashrate.

To repurpose for a similar project: edit `CONTRACT / CHAIN_ID / SEL` at the top of `hashdogos.py` (contract address + selectors, reverse-engineered from the project's frontend `/api/config` + one successful tx — don't guess), and verify the preimage layout against the frontend.

## ⚠️ Safety rules

- **Paid project**: minting spends real money, and the price rises with supply. Always **lock the price + set a spend cap + dry run first** so a price/difficulty spike can't overspend.
- **Use a burner wallet only**; keep the private key in a local environment variable — never hardcode it, paste it in chats, or upload it. The script only does "read contract + compute keccak + submit mine" — no transfers, no other signatures.
- **Reject variant scams**: if a project asks you to run *its executable*, or grind a vanity address with *its public key* (e.g. `profanity2 -z <pubkey>`) — that's grinding a private key for the scammer + baiting you to deposit. Hard pass. Only run open-source scripts you can read, only with your own wallet.

## License

[MIT](LICENSE) — provided as-is, the author is not responsible for any losses. Use at your own risk.
