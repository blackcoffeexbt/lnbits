const assert = require('node:assert/strict')
const {readFileSync} = require('node:fs')
const {resolve} = require('node:path')
const {test} = require('node:test')
const vm = require('node:vm')

function component(file, globals = {}) {
  let definition
  vm.runInNewContext(readFileSync(resolve(__dirname, '../../', file), 'utf8'), {
    window: {app: {component: (_name, value) => (definition = value)}},
    console,
    ...globals
  })
  const instance = {...definition.data()}
  for (const [name, method] of Object.entries(definition.methods)) {
    instance[name] = method.bind(instance)
  }
  return {definition, instance}
}

for (const failure of [false, true]) {
  test(`signing ${failure ? 'error stays visible' : 'success closes dialog'} and PIN clears`, async () => {
    const {instance: signer} = component(
      'lnbits/static/js/components/onchain/nostr-signer.js'
    )
    signer.ensureConnected = async () => {
      assert.equal(signer.dialog, false)
      assert.equal(signer.signingDialog, true)
    }
    signer.client = {
      sign: async () => {
        signer.pin = '123456'
        signer.pinRequired = true
        if (failure) throw new Error('Device rejected request')
        return 'signed-psbt'
      }
    }
    if (failure) {
      await assert.rejects(signer.signPsbt('psbt'), /Device rejected/)
    } else {
      assert.equal(await signer.signPsbt('psbt'), 'signed-psbt')
    }
    assert.equal(signer.dialog, false)
    assert.equal(signer.signingDialog, failure)
    assert.equal(signer.signing, false)
    assert.equal(signer.pinRequired, false)
    assert.equal(signer.pin, '')
  })
}

test('saved pairing reconnects without opening pairing setup', async () => {
  const {instance: signer} = component(
    'lnbits/static/js/components/onchain/nostr-signer.js'
  )
  signer.hasPairing = () => true
  signer.connect = async () => {
    assert.equal(signer.dialog, false)
    signer.connected = true
    signer.client = {}
  }
  await signer.ensureConnected()
  assert.equal(signer.dialog, false)
})

for (const configured of [false, true, 'different']) {
  test(`remote account import uses the core wallet API: ${configured}`, async () => {
    const calls = []
    const descriptor = 'wpkh([12345678/84h/1h/0h]public-key/<0;1>/*)#checksum'
    const {instance: signer} = component(
      'lnbits/static/js/components/onchain/nostr-signer.js',
      {
        LNbits: {
          api: {
            async request(method, path, key, body) {
              calls.push({method, path, key, body})
              return {
                data: configured
                  ? {
                      onchain_network: 'Testnet4',
                      onchain_wallet_kind: 'watch',
                      onchain_meta: {
                        masterpub:
                          configured === true
                            ? 'wpkh([12345678/84h/1h/0h]public-key/{0,1}/*)'
                            : 'other'
                      }
                    }
                  : {onchain_wallet_kind: null}
              }
            }
          }
        }
      }
    )
    signer.network = 'Testnet4'
    signer.adminkey = 'test-key'
    signer.account = {descriptor, path: "m/84'/1'/0'"}
    signer.$emit = () => {}
    signer.$q = {notify() {}}
    await signer.importAccount()
    assert.equal(calls[0].path, '/api/v1/wallet/onchain')
    assert.equal(calls[0].method, 'GET')
    assert.equal(calls.length, configured ? 1 : 2)
    assert.equal(signer.imported, configured !== 'different')
    if (!configured) {
      assert.equal(calls[1].method, 'POST')
      assert.equal(calls[1].path, '/api/v1/wallet/onchain')
      assert.equal(calls[1].body.meta.signer, 'nostr')
      assert.equal(calls[1].body.meta.accountPath, signer.account.path)
      assert.equal(
        calls[1].body.masterpub,
        'wpkh([12345678/84h/1h/0h]public-key/{0,1}/*)'
      )
    }
  })
}
