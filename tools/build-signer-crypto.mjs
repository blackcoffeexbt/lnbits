import {build} from 'esbuild'
import {readFileSync} from 'node:fs'
const check = process.argv.includes('--check')
for (const client of [false, true]) {
  const result = await build({
    write: !check,
    entryPoints: [
      client
        ? 'lnbits/static/js/onchain/nostr-signer-client.js'
        : 'lnbits/static/js/onchain/session-crypto.js'
    ],
    outfile: client
      ? 'lnbits/static/js/onchain/nostr-signer.bundle.js'
      : 'lnbits/static/js/onchain/session-crypto.bundle.js',
    bundle: true,
    format: client ? 'iife' : 'esm',
    globalName: client ? 'BitcoinRemoteSigner' : undefined,
    platform: 'browser',
    target: 'es2022',
    minify: true,
    legalComments: 'linked'
  })

  if (check) {
    for (const file of result.outputFiles) {
      if (!Buffer.from(file.contents).equals(readFileSync(file.path)))
        throw new Error(`Stale signer crypto bundle: ${file.path}`)
    }
  }
}
