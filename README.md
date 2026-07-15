# Gas-Optimized Clone Deployer (Base)

A single-file Python script that deploys **N** copies of a simple storage contract on **Base** (mainnet or Sepolia testnet) as cheaply as possible, using the **EIP-1167 minimal proxy** pattern.

Instead of deploying `N` full contracts (expensive), it:

1. Deploys one **implementation** contract (`SimpleStorage`) plus a **factory** (`SimpleStorageFactory`) that knows how to clone it.
2. Creates every additional instance as a tiny **EIP-1167 minimal proxy clone**, which costs a small fraction of a full deployment.
3. Batches clone creation (`deployBatch`) so many contracts are created per transaction instead of paying a base fee for each one individually.
4. Uses live **EIP-1559** fee data and `eth_estimateGas` (with a safety buffer) instead of hardcoded gas values.
5. Checks every transaction receipt and stops immediately on revert/failure, printing the tx hash.
6. Polls for contract bytecode before reading from a freshly deployed contract, to avoid spurious errors from lagging public RPC nodes.

## Requirements

- Python 3.9+
- Node.js + npm (only needed to fetch the OpenZeppelin contracts library used by `solc`)
- A Base or Base Sepolia RPC endpoint (public defaults are included)
- A wallet with a small amount of ETH (Base or Base Sepolia) to pay for gas

## Installation

Clone the repo and install dependencies:

```bash
git clone <your-repo-url>
cd <your-repo-folder>

# Python dependencies
pip install web3 py-solc-x python-dotenv

# Node dependency (used only so solc can resolve the OpenZeppelin import)
npm init -y
npm install @openzeppelin/contracts
```

> `py-solc-x` will download the Solidity compiler (`0.8.24`) automatically the first time you run the script.

## Configuration

On first run, the script creates a `.env` file next to `deploy.py` with default RPC URLs:

```
PRIVATE_KEY=
BASE_RPC_URL=https://mainnet.base.org
BASE_SEPOLIA_RPC_URL=https://sepolia.base.org
```

If `PRIVATE_KEY` is empty, the script will prompt you to paste it (input is hidden, not printed or transmitted anywhere else). It's saved locally to `.env` for reuse.

**Security notes:**
- Use a dedicated deployment wallet, not one holding significant funds.
- Add `.env` to your `.gitignore` — never commit it.
- Replace the public RPC URLs with a dedicated provider (e.g. Alchemy, Infura) if you hit rate limits or lag issues.

## Usage

```bash
python deploy.py
```

You'll be prompted for:

| Prompt | Description |
|---|---|
| Network | `base` (mainnet) or `baseSepolia` (testnet, default) |
| Number of contracts | How many `SimpleStorage` clones to deploy |
| Initial value | Starting `value` stored in each clone (default `0`) |

The script will then:

1. Compile `SimpleStorage` and `SimpleStorageFactory`.
2. Deploy the factory (which deploys the implementation contract in its constructor).
3. Deploy clones in batches of up to 50 per transaction until the requested count is reached.
4. Print the factory address, implementation address, and the full list of deployed clone addresses.

### Example output

```
Deployer: 0xAbC...
Balance: 0.05 ETH
How many contracts do you want to deploy? 120
Initial value to store in each contract (default 0): 0

Compiling contracts...
maxFeePerGas: 0.12 gwei
maxPriorityFeePerGas: 0.01 gwei

Deploying factory + implementation contract...
Factory deploy tx sent: 0x...
Factory deployed at: 0x...
Implementation (logic) contract at: 0x...

Deploying batch of 50 contract(s) in a single transaction...
Gas used: 1234567

...

Done. 120 contract(s) deployed:
  [0] 0x...
  [1] 0x...
  ...
```

## Contracts

### `SimpleStorage`
A minimal contract storing a `uint256 value` and an `owner`. Since clones don't run the implementation's constructor, initialization happens via `initialize(owner, initialValue)`, which is guarded against being called more than once.

### `SimpleStorageFactory`
- Deploys one `SimpleStorage` implementation in its constructor.
- `deploy(initialValue)` creates and initializes a single clone.
- `deployBatch(count, initialValue)` creates `count` clones in one transaction.
- `getDeployedContracts()` / `deployedCount()` return the clones created so far.

## Notes

- The batch size per transaction is capped at 50 (`CHUNK_SIZE`) to stay within reasonable gas-per-block limits; adjust in the script if needed for your target network.
- Gas prices use `2 × baseFee + priorityFee` for `maxFeePerGas`, which comfortably covers a couple of blocks of base fee movement without a large fixed overpay.
- This script and its contracts are provided as an example/template — review and test on `baseSepolia` before using real funds on mainnet.

## License

MIT (see contract headers — adjust as needed for your repo).
