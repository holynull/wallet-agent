#!/usr/bin/env node
const https = require('https');

const API_BASE = 'api.bridgers.xyz';

function request(path, data) {
    return new Promise((resolve, reject) => {
        const payload = JSON.stringify(data);
        const options = {
            hostname: API_BASE,
            path: path,
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(payload)
            }
        };

        const req = https.request(options, (res) => {
            let body = '';
            res.on('data', chunk => body += chunk);
            res.on('end', () => {
                try {
                    resolve(JSON.parse(body));
                } catch (e) {
                    reject(new Error(`Failed to parse response: ${body}`));
                }
            });
        });

        req.on('error', reject);
        req.write(payload);
        req.end();
    });
}

const commands = {
    getTokens: async () => {
        return await request('/api/exchangeRecord/getToken', {});
    },
    quote: async (args) => {
        // args: { fromTokenAddress, toTokenAddress, fromTokenAmount, fromTokenChain, toTokenChain, userAddr, equipmentNo, sourceFlag }
        return await request('/api/sswap/quote', args);
    },
    getCallData: async (args) => {
        return await request('/api/sswap/swap', args);
    },
    generateOrder: async (args) => {
        return await request('/api/exchangeRecord/updateDataAndStatus', args);
    },
    queryRecords: async (args) => {
        return await request('/api/exchangeRecord/getTransData', args);
    },
    queryDetails: async (args) => {
        return await request('/api/exchangeRecord/getTransDataById', args);
    }
};

async function main() {
    const args = process.argv.slice(2);
    if (args.length === 0) {
        console.log("Usage: node bridgers.js <command> [json_args]");
        console.log("Commands: getTokens, quote, getCallData, generateOrder, queryRecords, queryDetails");
        process.exit(1);
    }
    const command = args[0];
    const params = args[1] ? JSON.parse(args[1]) : {};
    if (!commands[command]) {
        console.error("Unknown command:", command);
        process.exit(1);
    }
    try {
        const result = await commands[command](params);
        console.log(JSON.stringify(result, null, 2));
    } catch (e) {
        console.error("Error:", e.message);
        process.exit(1);
    }
}

main();