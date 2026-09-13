---
name: hashdogos-keccak-pow-miner
description: Helps the user mine "on-chain keccak256 target-based proof-of-work" NFTs like HashDogos with a local NVIDIA GPU. Use when the user brings a project that requires mining/hashrate to mint and whose hash is keccak256 + target comparison (not leading-zero counting), e.g. hashdogos.fun.
---

# Local GPU mining for keccak256 target-based PoW (HashDogos-style)

The user has an NVIDIA GPU and wants to mine an "on-chain keccak256 PoW mint" NFT: find a nonce meeting the difficulty locally → submit `mine(nonce, anchorBlock)`, submitting with the user's wallet when found. **Every step either reuses this repo's `hashdogos.py` or copies directly from the project's frontend + one successful tx — never guess. After changing the kernel, always re-verify with eth_hash self-tests and simulate with eth_call before every submission; only go on-chain once it passes.**

## Difference from HashBroker (SHA-256 leading-zero style) — get this right first

| | HashBroker-style | **HashDogos-style (this skill)** |
|---|---|---|
| Hash algorithm | SHA-256 | **keccak256** (the Ethereum one) |
| Validity rule | leading zero bits ≥ difficulty | **hash < currentTarget(miner)** (integer comparison) |
| Difficulty | one global value | **per-miner-address** currentTarget(miner) |
| Preimage packing | tight concatenation | **standard abiEncode** (each field padded to 32 bytes) |
| Price | usually free | **paid, rising with global supply** |

Mixing them up wastes your hashrate: keccak≠sha256, target comparison≠zero counting — none of these can be wrong.

## Environment setup
1. Confirm the GPU: `nvidia-smi -L`. Install deps: `pip install pyopencl eth-account numpy eth-hash[pycryptodome]`.
2. Ask the user for: target contract, one **successful mint tx**, the project's frontend URL, and a **burner private key** for mining (small wallet only; the key stays in a local environment variable and is never shared).

## Reverse-engineering the PoW (copy the frontend + a successful tx, don't guess)
- **Frontends are usually SPAs**: `curl` the landing page to get JS chunk names, then pull `/_next/.../*miner*.js`. Many projects keep runtime config at **`GET /api/config`** (chain id, contract address, rpc).
- Find the preimage construction in the frontend JS. HashDogos is:
  `keccak256( abiEncode([address,uint256,address,uint256,bytes32,bytes32],
              [contract, chainId, miner, nonce, previousWork, anchor]) )`, valid when `hash < currentTarget(miner)`.
- Decode one **successful tx**: get `to` (contract), `value` (=mintPrice, note this is paid), selector, args. HashDogos submits `mine(uint256 nonce, uint256 anchorBlock)`, selector `0x071e9503`.
- Contract view functions: `mintPrice()`, `previousWork()` (bytes32, changes on every mint), `currentAnchor()` → (anchorBlock, anchor), `currentTarget(address)`. Selectors = `keccak(sig)[:4]` — compute them, don't memorize.
- **Verify the layout**: recompute `eth_hash.keccak(preimage)` for any nonce; the authoritative check is **pre-submit `eth_call` simulation of `mine(...)`** — a wrong solution/layout reverts, disproven at zero cost. Only proceed once the simulation doesn't revert.

## The miner (OpenCL keccak256, measured ~1.2 GH/s on a 5090)
> This directory ships a ready-made **`hashdogos.py`** (self-contained: OpenCL keccak256 + first-launch self-test + simulation gate + price lock + spend cap + submission). **Prefer using it directly** — only change the top-level `CONTRACT / CHAIN_ID / SEL` (contract and selectors) and the preimage layout.

Kernel essentials (if writing your own): the 192-byte preimage = 2 keccak blocks (rate 136); block-2 padding is keccak's `0x01`…`0x80` (not SHA3's 0x06). Only the nonce changes (word3 in the abiEncode) — bake every other lane in as constants. Validity check via `bswap64(state[0]) < (target>>192)` — because the target's low bits are all f, a smaller high-64 means a smaller integer; mathematically exact. keccak is much slower than SHA-256 (2× 24-round permutations); a 2^32 target takes a few seconds on a 5090.

## Main loop + safety gates (built into hashdogos.py; follow the same pattern if writing your own)
- **Paid-project warning**: minting spends real money (mintPrice ETH each), and the price rises with supply. **Compute the price and total cost for the user and get their confirmation before firing.** Paying = buying.
- **Price lock**: `PRICE_LOCK=<wei>` — stop the moment the price changes; never quietly pay more at the new price.
- **Spend cap**: `CAP_ETH` — stop when exceeded.
- **Dry run first**: without `DO=1`, only mine + simulate, no transactions. Run once to confirm the kernel self-test and simulation pass, then `DO=1` for real.
- Each round: read price/previousWork/anchor/currentTarget → GPU mine → recompute locally with eth_hash → **eth_call simulate** (the anchor has a freshness window; expired reverts → re-read and re-mine) → sign `mine(nonce,anchorBlock)` with value=price and send → after success, previousWork changed: re-read and mine the next one.
- **RPC requests must include a `user-agent` header** (no UA often gets 403). The submitting wallet needs enough for gas + the mint price; top it up first.

## Safety rules (must follow)
- Use a **burner** private key only; the key stays in a local environment variable and is never shared or pasted anywhere. The script only does "read contract + compute keccak + submit mine".
- ⚠️ **Reject variant scams**: if a project tells you to run **its executable**, or grind a vanity address with **its public key** (e.g. `profanity2 -z <pubkey>`) — that's grinding a private key for the scammer + baiting you to deposit. Hard pass. Only run open-source scripts you can read, only with your own wallet.
- For paid projects, always **lock the price + set a spend cap + dry run first** so a price/difficulty spike can't overspend.
