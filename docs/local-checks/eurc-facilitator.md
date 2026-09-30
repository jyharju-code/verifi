# Local check: EURC and USDC settle through the real facilitator

Contract v3 accepts EURC next to USDC. This check proves, without any mainnet
transaction, that the facilitator image Verifi runs settles an EURC
`transferWithAuthorization` produced by a standard x402 client through the
real verify-api gates.

Result on 2026-10-01: passed for EURC (admission and grace unlock) and USDC
(admission). No change to `deploy/facilitator-config.json` is needed.

## Setup

Everything runs on the operator's machine. Nothing touches Base mainnet
except read-only state fetched by the fork.

| Part | What ran |
| --- | --- |
| Chain | `anvil --fork-url https://mainnet.base.org --chain-id 8453` (foundry image `sha256:0c00cb0b...`), forked at block 0x319939f |
| Facilitator | `ghcr.io/x402-rs/x402-facilitator:latest`, label version 2.0.2 (`sha256:4ebe09d8...`), config identical to production except the RPC points at the fork |
| Facilitator signer | anvil's public dev account 0, funded with fork ETH only |
| Payer | a key generated for the run, given 10 EURC and 10 USDC on the fork with `anvil_dealERC20` |
| verify-api | `createApp()` from this branch, real `HTTPFacilitatorClient`, the fake core from `routes/testkit.js` |
| Client | `x402Client` with `ExactEvmScheme` from `@x402/evm` 2.18.0 |

## Steps

1. Start the fork and the facilitator on one docker network:

   ```sh
   docker network create v3fork
   docker run -d --name v3-anvil --network v3fork -p 127.0.0.1:18545:8545 \
     --entrypoint anvil ghcr.io/foundry-rs/foundry:latest \
     --host 0.0.0.0 --fork-url https://mainnet.base.org --chain-id 8453 --no-rate-limit
   docker run -d --name v3-facilitator --network v3fork -p 127.0.0.1:18402:8080 \
     -v "$PWD/facilitator-fork.json:/app/config.json:ro" -e CONFIG=/app/config.json \
     -e FACILITATOR_PRIVATE_KEY=<anvil dev account 0 key> \
     ghcr.io/x402-rs/x402-facilitator:latest
   ```

   `facilitator-fork.json` is `deploy/facilitator-config.json` with the
   `eip155:8453` RPC set to `http://v3-anvil:8545` and the Sepolia chain
   removed. `/supported` must list `exact` on `eip155:8453`.

2. Give a fresh payer tokens on the fork:
   `anvil_dealERC20(payer, EURC, 10000000)` and the same for USDC.

3. Run verify-api in process with the real facilitator client and a fake core
   holding one quote (`st_fork`, rate 1.17). The payer calls `POST /verify`,
   reads the two `accepts` entries, signs one, and repeats the request.

4. For the unlock, the fake core returns the same chain as ready in the grace
   window; the payer calls `POST /verify-unlock` and pays its single
   requirement.

## Observed

```
gate 1 402: 402 USDC 120000, EURC 100000
gate 1 paid: 202 tx 0xcf189c31...
EURC payer 10000000 -> 9900000 | payTo 0 -> 100000
unlock 402: 402 EURC 1450000
unlock paid: 200 completed accepted
EURC payer 9900000 -> 8450000 | payTo 100000 -> 1550000
recorded with core: entry 0xcf189c310e, unlock 0x82c9db96fa

gate 1 402: 402 USDC 120000, EURC 100000
gate 1 paid: 202 tx 0x267a26dd...
USDC payer 10000000 -> 9880000 | payTo 0 -> 120000
```

The EIP-712 domain the facilitator used came from the requirement's `extra`
(`EURC`, version `2`), which matches the token contract read on-chain
(`docs/plans/contract-v3-three-gate.md`, section 1.5).

## Clean up

```sh
docker rm -f v3-anvil v3-facilitator && docker network rm v3fork
```
