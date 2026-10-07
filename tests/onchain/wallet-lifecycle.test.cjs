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
    setTimeout: () => 1,
    clearTimeout: () => {},
    ...globals
  })
  const instance = {...definition.data()}
  for (const [name, method] of Object.entries(definition.methods)) {
    instance[name] = method.bind(instance)
  }
  return {definition, instance}
}

for (const failure of [false, true]) {
  test(`signing ${failure ? 'error stays visible' : 'success shows confirmation before closing'} and PIN clears`, async () => {
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
    assert.equal(signer.signingDialog, true)
    assert.equal(signer.signingComplete, !failure)
    assert.equal(signer.signingError, failure)
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

test('decryption and device approval never complete signing without a result', () => {
  const {definition, instance: signer} = component(
    'lnbits/static/js/components/onchain/nostr-signer.js'
  )
  signer.signing = true
  for (const status of [
    'Decrypting wallet',
    'Ready to sign — approve on device',
    'Signing complete'
  ]) {
    signer.message = status
    const progress = definition.computed.progress.call(signer)
    assert.ok(progress.step < 4)
    assert.equal(signer.signingComplete, false)
    if (status.includes('approve')) assert.equal(progress.icon, 'warning')
  }
  signer.message =
    'On-device authorisation required. Wallet period limit exceeded'
  const approval = definition.computed.progress.call(signer)
  assert.equal(approval.icon, 'warning')
  assert.equal(approval.title, 'On-device authorisation required')
  assert.equal(approval.detail, 'Wallet period limit exceeded')
  signer.signingError = true
  signer.message = 'Incorrect PIN: 2 attempts remaining'
  const progress = definition.computed.progress.call(signer)
  assert.equal(progress.icon, 'warning')
  assert.match(progress.detail, /2 attempts remaining/)
})

test('success waits for transaction review preparation and surfaces failures', async () => {
  const {instance: signer} = component(
    'lnbits/static/js/components/onchain/nostr-signer.js'
  )
  signer.ensureConnected = async () => {}
  signer.client = {sign: async () => 'signed-psbt'}
  await assert.rejects(
    signer.signPsbt('psbt', async signed => {
      assert.equal(signed, 'signed-psbt')
      assert.equal(signer.signingComplete, false)
      throw new Error('Invalid transaction')
    }),
    /Invalid transaction/
  )
  assert.equal(signer.signingComplete, false)
  assert.equal(signer.signingError, true)
  assert.equal(signer.signingDialog, true)
})

test('saved pairing status survives disconnect and removal updates wallet controls', () => {
  let saved = 'paired'
  const {instance: signer} = component(
    'lnbits/static/js/components/onchain/nostr-signer.js',
    {
      localStorage: {
        getItem: () => saved,
        removeItem: () => {
          saved = null
        }
      }
    }
  )
  const events = []
  signer.$emit = (name, value) => events.push([name, value])
  signer.$q = {notify() {}}
  signer.refreshPairing()
  signer.disconnect()
  assert.equal(signer.paired, true)
  signer.forget()
  assert.equal(signer.paired, false)
  assert.deepEqual(events, [
    ['pairing-changed', true],
    ['pairing-changed', false]
  ])
})

test('hiding during device approval preserves signing, clears PIN, and closes three seconds after review', async () => {
  let visibilityChanged
  let finishSigning
  let finishReview
  let closeSuccess
  let closeDelay
  let closes = 0
  const document = {
    hidden: false,
    addEventListener: (_, handler) => {
      visibilityChanged = handler
    }
  }
  const {definition, instance: signer} = component(
    'lnbits/static/js/components/onchain/nostr-signer.js',
    {
      document,
      setTimeout: (callback, delay) => {
        closeSuccess = callback
        closeDelay = delay
        return 1
      }
    }
  )
  const signedResult = new Promise(resolve => {
    finishSigning = resolve
  })
  const reviewReady = new Promise(resolve => {
    finishReview = resolve
  })
  const client = {
    sign: () => signedResult,
    close: () => {
      closes++
    }
  }
  signer.client = client
  signer.ensureConnected = async () => {}
  definition.mounted.call(signer)
  let preparingReview = false
  const signing = signer.signPsbt('psbt', async signed => {
    assert.equal(signed, 'signed-psbt')
    preparingReview = true
    await reviewReady
  })
  await Promise.resolve()
  signer.pin = '123456'
  signer.message = 'Ready to sign — approve on device'
  document.hidden = true
  visibilityChanged()
  assert.equal(signer.pin, '')
  assert.equal(closes, 0)
  assert.equal(signer.client, client)
  assert.equal(
    definition.computed.progress.call(signer).title,
    'Ready to sign — approve on device'
  )
  finishSigning('signed-psbt')
  await new Promise(setImmediate)
  assert.equal(preparingReview, true)
  assert.equal(signer.signingDialog, true)
  finishReview()
  assert.equal(await signing, 'signed-psbt')
  assert.equal(signer.signingDialog, true)
  assert.equal(closeDelay, 3000)
  assert.equal(signer.signingComplete, true)
  closeSuccess()
  assert.equal(signer.signingDialog, false)
  assert.equal(signer.successCloseTimer, null)
  assert.equal(signer.signingError, false)
  assert.equal(closes, 1)
})

test('unmount still disconnects an active signing session', () => {
  let closes = 0
  let removed = false
  const {definition, instance: signer} = component(
    'lnbits/static/js/components/onchain/nostr-signer.js',
    {
      document: {
        removeEventListener: () => {
          removed = true
        }
      }
    }
  )
  signer.signing = true
  signer.client = {
    close: () => {
      closes++
    }
  }
  signer.pin = '123456'
  definition.beforeUnmount.call(signer)
  assert.equal(closes, 1)
  assert.equal(removed, true)
  assert.equal(signer.pin, '')
})

test('starting another request cancels the previous success auto-close', async () => {
  const cancelled = []
  const {instance: signer} = component(
    'lnbits/static/js/components/onchain/nostr-signer.js',
    {
      setTimeout: () => 42,
      clearTimeout: timer => cancelled.push(timer)
    }
  )
  signer.ensureConnected = async () => {}
  signer.client = {sign: async () => 'signed-psbt'}
  await signer.signPsbt('first')
  assert.equal(signer.successCloseTimer, 42)
  signer.client.sign = async () => {
    throw new Error('Rejected')
  }
  await assert.rejects(signer.signPsbt('second'), /Rejected/)
  assert.deepEqual(cancelled, [42])
  assert.equal(signer.successCloseTimer, null)
  assert.equal(signer.signingDialog, true)
})

test('unmount cancels a pending success auto-close', () => {
  const cancelled = []
  const {definition, instance: signer} = component(
    'lnbits/static/js/components/onchain/nostr-signer.js',
    {
      document: {removeEventListener() {}},
      clearTimeout: timer => cancelled.push(timer)
    }
  )
  signer.successCloseTimer = 42
  definition.beforeUnmount.call(signer)
  assert.deepEqual(cancelled, [42])
  assert.equal(signer.successCloseTimer, null)
})

for (const failed of [false, true]) {
  test(`stop waiting ${failed ? 'keeps the modal on unconfirmed cancellation' : 'closes after confirmed cancellation'}`, async () => {
    const {instance: signer} = component(
      'lnbits/static/js/components/onchain/nostr-signer.js'
    )
    signer.signingDialog = true
    signer.pin = '123456'
    let acknowledge
    const acknowledgement = new Promise((resolve, reject) => {
      acknowledge = failed ? reject : resolve
    })
    signer.client = {cancel: () => acknowledgement, close() {}}
    signer.$q = {notify() {}}
    const stopping = signer.stopWaiting()
    assert.equal(signer.signingDialog, true)
    assert.equal(signer.stopping, true)
    assert.equal(signer.pin, '')
    acknowledge(failed ? new Error('Cancellation not confirmed') : true)
    await stopping
    assert.equal(signer.signingDialog, failed)
    assert.equal(signer.stopping, false)
  })
}
