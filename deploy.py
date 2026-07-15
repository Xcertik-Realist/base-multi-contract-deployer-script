"""
Single-file script to deploy N gas-optimized clone contracts on Base.
 
Strategy:
1. Compile an embedded SimpleStorage (logic) contract and SimpleStorageFactory.
2. Deploy the factory once. Its constructor deploys ONE copy of SimpleStorage
   ("the implementation").
3. Every additional contract instance is an EIP-1167 minimal proxy clone,
   costing a small fraction of a full deployment.
4. Clones are created via factory.deployBatch(), so many contracts are
   created in a single transaction instead of paying the ~21,000 gas base
   fee once per contract.
5. Gas fees use live EIP-1559 fee data from the network, and gas limits
   come from eth_estimateGas per batch with a safety buffer.
6. Every transaction's receipt status is checked — if a tx reverts or runs
   out of gas, the script stops immediately with the tx hash.
7. After deploying, the script polls for contract bytecode to actually be
   visible before reading from it — public RPC nodes can lag behind the
   node that confirmed your transaction, otherwise causing a spurious
   "empty return data" error on a freshly deployed contract.
 
Setup:
    pip install web3 py-solc-x python-dotenv
    npm init -y
    npm install @openzeppelin/contracts
    python deploy.py
"""
 
import os
import sys
import time
import getpass
from pathlib import Path
 
from dotenv import load_dotenv, set_key
from web3 import Web3
from solcx import compile_standard, install_solc, set_solc_version
 
# --------------------------------------------------------------------------
# Embedded contract source
# --------------------------------------------------------------------------
 
SIMPLE_STORAGE_SOURCE = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
 
/// @title SimpleStorage
/// @notice Minimal example contract. Deployed once as an "implementation" and then
///         copied cheaply via EIP-1167 minimal proxy clones (see SimpleStorageFactory).
/// @dev Clones do NOT run the constructor of the implementation, so initialization
///      logic lives in `initialize()` instead, guarded against being called twice.
contract SimpleStorage {
    uint256 public value;
    address public owner;
    bool private initialized;
 
    event Initialized(address indexed owner, uint256 initialValue);
    event ValueChanged(uint256 newValue);
 
    function initialize(address _owner, uint256 _initialValue) external {
        require(!initialized, "Already initialized");
        initialized = true;
        owner = _owner;
        value = _initialValue;
        emit Initialized(_owner, _initialValue);
    }
 
    function setValue(uint256 _newValue) external {
        require(msg.sender == owner, "Not owner");
        value = _newValue;
        emit ValueChanged(_newValue);
    }
}
"""
 
FACTORY_SOURCE = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
 
import "@openzeppelin/contracts/proxy/Clones.sol";
import "./SimpleStorage.sol";
 
/// @title SimpleStorageFactory
/// @notice Deploys the SimpleStorage logic once, then hands out cheap
///         EIP-1167 minimal-proxy clones of it.
contract SimpleStorageFactory {
    address public immutable implementation;
    address[] public deployedContracts;
 
    event ContractDeployed(address indexed clone, address indexed owner, uint256 initialValue);
 
    constructor() {
        implementation = address(new SimpleStorage());
    }
 
    function deploy(uint256 _initialValue) public returns (address clone) {
        clone = Clones.clone(implementation);
        SimpleStorage(clone).initialize(msg.sender, _initialValue);
        deployedContracts.push(clone);
        emit ContractDeployed(clone, msg.sender, _initialValue);
    }
 
    function deployBatch(uint256 _count, uint256 _initialValue) external returns (address[] memory clones) {
        clones = new address[](_count);
        for (uint256 i = 0; i < _count; i++) {
            clones[i] = deploy(_initialValue);
        }
    }
 
    function getDeployedContracts() external view returns (address[] memory) {
        return deployedContracts;
    }
 
    function deployedCount() external view returns (uint256) {
        return deployedContracts.length;
    }
}
"""
 
SOLC_VERSION = "0.8.24"
CHUNK_SIZE = 50  # max contracts deployed per transaction
ENV_PATH = Path(__file__).parent / ".env"
 
 
# --------------------------------------------------------------------------
# .env handling
# --------------------------------------------------------------------------
 
def ensure_env():
    """Create .env with defaults if missing, and prompt for a private key
    if one isn't set yet. The key is written only to your local .env file,
    never printed or sent anywhere else."""
    if not ENV_PATH.exists():
        ENV_PATH.write_text(
            "PRIVATE_KEY=\n"
            "BASE_RPC_URL=https://mainnet.base.org\n"
            "BASE_SEPOLIA_RPC_URL=https://sepolia.base.org\n"
        )
        print(f"Created {ENV_PATH}")
 
    load_dotenv(ENV_PATH)
 
    if not os.getenv("PRIVATE_KEY"):
        print("\nNo PRIVATE_KEY found in .env.")
        print("Paste the private key of the wallet you want to deploy from.")
        print("Use a dedicated deployment wallet, not one holding significant funds.")
        key = getpass.getpass("Private key (input hidden): ").strip()
        if not key:
            sys.exit("A private key is required to deploy.")
        if not key.startswith("0x"):
            key = "0x" + key
        set_key(str(ENV_PATH), "PRIVATE_KEY", key)
        os.environ["PRIVATE_KEY"] = key
        print(f"Saved PRIVATE_KEY to {ENV_PATH}. Keep this file out of version control.")
 
    load_dotenv(ENV_PATH, override=True)
 
 
# --------------------------------------------------------------------------
# Input helpers
# --------------------------------------------------------------------------
 
def prompt_int(prompt_text, default=0):
    """Repeatedly ask until the user enters a whole number, or presses
    Enter to accept the default. Rejects decimals like '0.001'."""
    while True:
        raw = input(prompt_text).strip()
        if raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            print("Please enter a whole number (e.g. 0, 1, 42) — no decimals.")
 
 
# --------------------------------------------------------------------------
# Transaction / RPC-lag helpers
# --------------------------------------------------------------------------
 
def send_and_check(w3: Web3, tx: dict, private_key: str, label: str):
    """Sign, send, and wait for a transaction — then verify it actually
    succeeded instead of assuming success just because it got mined."""
    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"{label} tx sent: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    if receipt.status != 1:
        sys.exit(
            f"{label} transaction FAILED (reverted or ran out of gas).\n"
            f"Tx hash: {tx_hash.hex()}\n"
            f"Check it on BaseScan for the revert reason."
        )
    return receipt
 
 
def wait_for_code(w3: Web3, address: str, timeout: int = 60, poll_interval: int = 2):
    """Poll until bytecode is visible at `address`. Public RPC nodes can lag
    behind the node that confirmed your transaction, so calling a freshly
    deployed contract immediately can otherwise fail with empty return data."""
    elapsed = 0
    while elapsed < timeout:
        code = w3.eth.get_code(address)
        if code and code != b"":
            return
        time.sleep(poll_interval)
        elapsed += poll_interval
    sys.exit(
        f"Timed out waiting for contract code at {address}.\n"
        f"The public RPC endpoint may be lagging. Try again in a minute, "
        f"or switch to a dedicated RPC provider (e.g. Alchemy/Infura) in .env."
    )
 
 
# --------------------------------------------------------------------------
# Compilation
# --------------------------------------------------------------------------
 
def compile_contracts():
    install_solc(SOLC_VERSION)
    set_solc_version(SOLC_VERSION)
 
    node_modules = Path(__file__).parent / "node_modules"
    if not (node_modules / "@openzeppelin").exists():
        sys.exit(
            "Missing @openzeppelin/contracts.\n"
            "Run: npm init -y && npm install @openzeppelin/contracts"
        )
 
    remappings = [f"@openzeppelin/={node_modules / '@openzeppelin'}/"]
 
    compiled = compile_standard(
        {
            "language": "Solidity",
            "sources": {
                "SimpleStorage.sol": {"content": SIMPLE_STORAGE_SOURCE},
                "SimpleStorageFactory.sol": {"content": FACTORY_SOURCE},
            },
            "settings": {
                "optimizer": {"enabled": True, "runs": 200},
                "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object"]}},
                "remappings": remappings,
            },
        },
        allow_paths=str(node_modules),
    )
    return compiled
 
 
def get_contract(compiled, filename, contract_name):
    data = compiled["contracts"][filename][contract_name]
    return data["abi"], data["evm"]["bytecode"]["object"]
 
 
# --------------------------------------------------------------------------
# Gas fee optimization
# --------------------------------------------------------------------------
 
def get_optimized_fees(w3: Web3):
    latest = w3.eth.get_block("latest")
    base_fee = latest["baseFeePerGas"]
    priority_fee = w3.eth.max_priority_fee
    # Modest buffer over 2x base fee protects against the next couple of
    # blocks moving against you, without overpaying like a large fixed
    # multiplier would.
    max_fee = base_fee * 2 + priority_fee
    return max_fee, priority_fee
 
 
# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
 
def main():
    ensure_env()
 
    private_key = os.getenv("PRIVATE_KEY")
 
    network = input("Deploy to 'base' (mainnet) or 'baseSepolia' (testnet)? [baseSepolia]: ").strip() or "baseSepolia"
    rpc_url = os.getenv("BASE_RPC_URL") if network == "base" else os.getenv("BASE_SEPOLIA_RPC_URL")
    if not rpc_url:
        sys.exit(f"No RPC URL set for '{network}' in .env")
 
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        sys.exit(f"Could not connect to {rpc_url}")
 
    account = w3.eth.account.from_key(private_key)
    print(f"\nDeployer: {account.address}")
    balance = w3.eth.get_balance(account.address)
    print(f"Balance: {w3.from_wei(balance, 'ether')} ETH")
 
    count = prompt_int("How many contracts do you want to deploy? ")
    if count <= 0:
        sys.exit("Count must be a positive integer.")
 
    initial_value = prompt_int("Initial value to store in each contract (default 0): ", default=0)
 
    print("\nCompiling contracts...")
    compiled = compile_contracts()
    factory_abi, factory_bytecode = get_contract(compiled, "SimpleStorageFactory.sol", "SimpleStorageFactory")
 
    max_fee, priority_fee = get_optimized_fees(w3)
    print(f"maxFeePerGas: {w3.from_wei(max_fee, 'gwei')} gwei")
    print(f"maxPriorityFeePerGas: {w3.from_wei(priority_fee, 'gwei')} gwei")
 
    nonce = w3.eth.get_transaction_count(account.address)
 
    # --- Deploy the factory (which deploys the implementation once in its constructor) ---
    print("\nDeploying factory + implementation contract...")
    Factory = w3.eth.contract(abi=factory_abi, bytecode=factory_bytecode)
    estimated_gas = Factory.constructor().estimate_gas({"from": account.address})
 
    tx = Factory.constructor().build_transaction({
        "from": account.address,
        "nonce": nonce,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": priority_fee,
        "gas": int(estimated_gas * 1.3),  # bigger buffer — constructor deploys a nested contract
    })
    receipt = send_and_check(w3, tx, private_key, "Factory deploy")
    factory_address = receipt.contractAddress
    print(f"Factory deployed at: {factory_address}")
    nonce += 1
 
    print("Waiting for factory bytecode to be visible on this RPC node...")
    wait_for_code(w3, factory_address)
 
    factory = w3.eth.contract(address=factory_address, abi=factory_abi)
    implementation_address = factory.functions.implementation().call()
    print(f"Implementation (logic) contract at: {implementation_address}")
 
    # --- Deploy clones in batches ---
    remaining = count
 
    while remaining > 0:
        batch = min(CHUNK_SIZE, remaining)
        print(f"\nDeploying batch of {batch} contract(s) in a single transaction...")
 
        fn = factory.functions.deployBatch(batch, initial_value)
        estimated_gas = fn.estimate_gas({"from": account.address})
 
        tx = fn.build_transaction({
            "from": account.address,
            "nonce": nonce,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority_fee,
            "gas": int(estimated_gas * 1.2),
        })
        receipt = send_and_check(w3, tx, private_key, f"Batch deploy (size {batch})")
        print(f"Gas used: {receipt.gasUsed}")
 
        nonce += 1
        remaining -= batch
 
    print("\nWaiting briefly before reading final deployed contract list...")
    time.sleep(2)
 
    all_deployed = factory.functions.getDeployedContracts().call()
    print(f"\nDone. {len(all_deployed)} contract(s) deployed:")
    for i, addr in enumerate(all_deployed):
        print(f"  [{i}] {addr}")
 
 
if __name__ == "__main__":
    main()
