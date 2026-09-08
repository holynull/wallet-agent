const fs = require('fs');
const path = require('path');
require('dotenv').config({ path: path.join(__dirname, '../assets/.env') });
const { ethers } = require('ethers');

const RPC_MAP = {
    'ETH': 'https://eth.llamarpc.com',
    'BSC': 'https://binance.llamarpc.com',
    'ARB': 'https://rpc.ankr.com/arbitrum',
    'POLYGON': 'https://polygon-rpc.com',
    'Optimism': 'https://mainnet.optimism.io',
    'AVAXC': 'https://api.avax.network/ext/bc/C/rpc',
    'BASE': 'https://mainnet.base.org',
    'LINEA': 'https://rpc.linea.build',
    'CRONOS': 'https://evm.cronos.org',
    'CELO': 'https://forno.celo.org',
    'GNOSIS': 'https://rpc.gnosischain.com',
    'Moonriver': 'https://rpc.api.moonriver.moonbeam.network',
    'zkEVM': 'https://zkevm-rpc.com'
};

const ERC20_ABI = [
    "function transfer(address to, uint256 amount) returns (bool)",
    "function decimals() view returns (uint8)"
];

async function autoTransfer({ network, tokenAddress, toAddress, amount, decimals }) {
    if (!process.env.EVM_PRIVATE_KEY) {
        throw new Error('EVM_PRIVATE_KEY not found in assets/.env. Please configure it to enable auto-transfer.');
    }

    const rpcUrl = RPC_MAP[network];
    if (!rpcUrl) {
        throw new Error(`Auto-transfer is currently not supported for network: ${network}. Please transfer manually.`);
    }

    const provider = new ethers.JsonRpcProvider(rpcUrl);
    const wallet = new ethers.Wallet(process.env.EVM_PRIVATE_KEY, provider);

    console.log(`[Auto-Transfer] Network: ${network}`);
    console.log(`[Auto-Transfer] Sending ${amount} to ${toAddress}`);

    let tx;
    try {
        if (!tokenAddress || tokenAddress.toLowerCase() === 'native' || tokenAddress === '') {
            // Native transfer
            const parsedAmount = ethers.parseUnits(amount.toString(), decimals ? parseInt(decimals) : 18);
            const txReq = {
                to: toAddress,
                value: parsedAmount
            };
            tx = await wallet.sendTransaction(txReq);
        } else {
            // ERC20 transfer
            const tokenContract = new ethers.Contract(tokenAddress, ERC20_ABI, wallet);
            const tokenDecimals = decimals ? parseInt(decimals) : await tokenContract.decimals();
            const parsedAmount = ethers.parseUnits(amount.toString(), tokenDecimals);
            tx = await tokenContract.transfer(toAddress, parsedAmount);
        }

        console.log(`[Auto-Transfer] Transaction submitted. Hash: ${tx.hash}`);
        console.log(`[Auto-Transfer] Waiting for confirmation...`);
        const receipt = await tx.wait();
        console.log(`[Auto-Transfer] Transaction confirmed in block ${receipt.blockNumber}!`);
        
        return {
            status: 'success',
            hash: tx.hash,
            network: network
        };

    } catch (error) {
        throw new Error(`Transfer failed: ${error.message}`);
    }
}

// Example usage when run directly from command line
if (require.main === module) {
    const args = process.argv.slice(2).reduce((acc, arg) => {
        const [key, value] = arg.split('=');
        if (key && value) acc[key] = value;
        return acc;
    }, {});

    if (!args.network || !args.toAddress || !args.amount) {
        console.error('Usage: node evm_transfer.js network=ARB tokenAddress=native toAddress=0x... amount=100 decimals=18');
        process.exit(1);
    }

    autoTransfer(args)
        .then(result => console.log(JSON.stringify(result, null, 2)))
        .catch(error => console.error(JSON.stringify({ status: 'error', message: error.message }, null, 2)));
}

module.exports = { autoTransfer };
