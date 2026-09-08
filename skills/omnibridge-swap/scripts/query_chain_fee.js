#!/usr/bin/env node

const https = require('https');

// Helper function for making HTTP POST requests
function postRequest(url, payload) {
    return new Promise((resolve, reject) => {
        const payloadString = JSON.stringify(payload);
        const urlObj = new URL(url);

        const options = {
            hostname: urlObj.hostname,
            port: urlObj.port || (urlObj.protocol === 'https:' ? 443 : 80),
            path: urlObj.pathname,
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(payloadString)
            }
        };

        const req = https.request(options, (res) => {
            let data = '';
            res.on('data', (chunk) => {
                data += chunk;
            });
            res.on('end', () => {
                try {
                    resolve(JSON.parse(data));
                } catch (e) {
                    reject(new Error(`Failed to parse JSON response: ${data}`));
                }
            });
        });

        req.on('error', (e) => {
            reject(new Error(`HTTP request failed: ${e.message}`));
        });

        req.write(payloadString);
        req.end();
    });
}

async function queryChainFee(params) {
    const API_ENDPOINT = 'https://api.omnibridge.pro/api/v1/chainFeeList';

    // Validate required parameters
    const requiredParams = ['coinCode'];

    for (const param of requiredParams) {
        if (!params[param]) {
            throw new Error(`Missing required parameter: ${param}`);
        }
    }

    const payload = {
        coinCode: params.coinCode
    };

    try {
        const response = await postRequest(API_ENDPOINT, payload);
        if (response.resCode === '800') {
            return {
                status: 'success',
                message: response.resMsg,
                chainFee: response.data
            };
        } else {
            return {
                status: 'error',
                message: response.resMsg || 'Unknown error',
                details: response
            };
        }
    } catch (error) {
        return {
            status: 'error',
            message: `API call failed: ${error.message}`
        };
    }
}

// Example usage when run directly from command line
if (require.main === module) {
    const args = process.argv.slice(2).reduce((acc, arg) => {
        const [key, value] = arg.split('=');
        if (key && value) acc[key] = value;
        return acc;
    }, {});

    queryChainFee(args)
        .then(result => console.log(JSON.stringify(result, null, 2)))
        .catch(error => console.error(JSON.stringify({ status: 'error', message: error.message }, null, 2)));
}

module.exports = { queryChainFee };