window.app.component('onchain-nostr-signer', {
  template: '#onchain-nostr-signer',
  props: ['network', 'user-id', 'wallet-id', 'adminkey'],
  emits: ['device:connected', 'wallet-imported', 'pairing-changed'],
  data() {
    return {
      dialog: false,
      paired: false,
      signingComplete: false,
      signerResultReceived: false,
      successCloseTimer: null,
      signingError: false,
      signingDialog: false,
      pairing: '',
      label: 'LNbits browser',
      client: null,
      connected: false,
      imported: false,
      busy: false,
      stopping: false,
      account: null,
      message: '',
      scan: false,
      signing: false,
      pin: '',
      pinRequired: false,
      pinBusy: false
    }
  },
  computed: {
    progress() {
      if (this.signingComplete)
        return {
          step: 4,
          icon: 'check',
          title: 'Transaction signed',
          detail: 'Review your transaction before broadcasting.'
        }
      if (this.signingError)
        return {
          step: 0,
          icon: 'warning',
          title: 'Signing needs attention',
          detail: this.message
        }
      if (this.pinRequired)
        return {
          step: 0,
          icon: 'lock',
          title: 'Unlock your wallet',
          detail: 'Enter your wallet decryption PIN below.'
        }
      if (this.message === 'Decrypting wallet')
        return {
          step: 1,
          icon: 'lock_open',
          title: 'Decrypting wallet',
          detail: 'This usually takes around 30 seconds. Keep this window open.'
        }
      if (
        /approve on device|review approvals|authorisations? required|approval required/i.test(
          this.message
        )
      )
        return {
          step: 2,
          icon: 'warning',
          title: this.message.split('. ')[0],
          detail: this.message.split('. ').slice(1).join('. ')
        }
      if (this.message === 'Signing complete')
        return {
          step: 3,
          icon: 'shield',
          title: 'Receiving signed transaction',
          detail: 'Waiting for the signed transaction from your device…'
        }
      if (/Validating|Signing|approved/.test(this.message))
        return {
          step: 2,
          icon: 'edit',
          title: this.message,
          detail: 'Your signer is checking and signing the transaction.'
        }
      return {
        step: 0,
        icon: 'lock',
        title: this.message || 'Connecting to signer…',
        detail: 'Keep your signer online and this window open.'
      }
    },
    dialogOpen: {
      get() {
        return this.dialog || this.signingDialog
      },
      set(value) {
        this.dialog = value
        if (!value) {
          this.signingDialog = false
          this.scan = false
        }
      }
    },
    storageKey() {
      return `lnbits:bitcoin-signer:v1:${this.userId}:${this.walletId}`
    },
    isNostrSigner() {
      return true
    }
  },
  watch: {
    network() {
      this.disconnect()
    },
    storageKey() {
      this.disconnect()
      this.refreshPairing()
    }
  },
  methods: {
    async stopWaiting() {
      if (this.stopping || this.signerResultReceived || !this.client) return
      this.stopping = true
      this.pin = ''
      try {
        await this.client.cancel()
        this.disconnect()
        this.message = 'Request cancelled. Your signer is ready.'
        this.dialog = false
        this.signingDialog = false
      } catch (error) {
        this.$q.notify({type: 'warning', message: error.message})
      } finally {
        this.stopping = false
      }
    },
    refreshPairing() {
      this.paired = this.hasPairing()
      this.$emit('pairing-changed', this.paired)
    },
    hasPairing() {
      return !!localStorage.getItem(this.storageKey)
    },
    isConnected() {
      return this.connected && ['Mainnet', 'Testnet4'].includes(this.network)
    },
    isAuthenticated() {
      return this.isConnected()
    },
    isTaprootSupported() {
      return false
    },
    async hwwShowPasswordDialog() {
      await this.ensureConnected()
    },
    async isAuthenticating() {
      return this.isAuthenticated()
    },
    async ensureConnected() {
      if (!this.connected) {
        if (!this.hasPairing()) this.dialog = true
        await this.connect(false, true)
      }
      if (!this.client || !this.connected)
        throw new Error(this.message || 'Signer unavailable')
    },
    async signPsbt(psbt, prepareReview) {
      if (this.signing) throw new Error('A signing request is already active')
      if (this.successCloseTimer !== null) {
        clearTimeout(this.successCloseTimer)
        this.successCloseTimer = null
      }
      this.signingComplete = false
      this.signerResultReceived = false
      this.signingError = false
      this.signing = true
      this.dialog = false
      this.signingDialog = true
      this.message = 'Connecting to signer…'
      try {
        await this.ensureConnected()
        this.message = 'Waiting for device…'
        const signed = await this.client.sign(psbt)
        this.signerResultReceived = true
        this.message = 'Signing complete'
        if (prepareReview) await prepareReview(signed)
        this.signingComplete = true
        this.successCloseTimer = setTimeout(() => {
          this.signingDialog = false
          this.successCloseTimer = null
        }, 3000)
        return signed
      } catch (error) {
        this.signingError = true
        this.message = error.message
        throw error
      } finally {
        this.signing = false
        this.pinRequired = false
        this.pin = ''
        if (
          !this.stopping &&
          typeof document !== 'undefined' &&
          document.hidden
        )
          this.disconnect()
      }
    },
    async submitPin() {
      this.pinBusy = true
      this.pinRequired = false
      this.signingError = false
      this.message = 'PIN submitted — waiting for signer…'
      let pin = this.pin
      this.pin = ''
      try {
        const submission = this.client.submitPin(pin)
        pin = ''
        await submission
      } catch (error) {
        this.signingError = true
        this.message = error.message
        this.pinRequired =
          this.signing &&
          [...(this.client?.pending.values() || [])].some(
            p => p.method === 'sign_psbt' && p.status === 'PIN required'
          )
      } finally {
        pin = ''
        this.pinBusy = false
      }
    },
    disconnect() {
      this.pin = ''
      this.pinRequired = false
      this.client?.close()
      this.client = null
      this.account = null
      this.connected = false
      this.imported = false
    },
    forget() {
      this.disconnect()
      localStorage.removeItem(this.storageKey)
      this.refreshPairing()
      this.pairing = ''
      this.scan = false
      this.$q.notify({
        type: 'info',
        message:
          'Pairing removed from this browser. Revoke it on the signer too.'
      })
      this.message = 'Forgot this browser pairing. Revoke it on the device too.'
    },
    async connect(pair = false, forSigning = false) {
      if (this.busy) return
      this.busy = true
      this.message = 'Connecting to signer…'
      try {
        if (!['Mainnet', 'Testnet4'].includes(this.network))
          throw new Error('Select Mainnet or Testnet4 first')
        this.disconnect()
        let saved
        if (pair) {
          const data = BitcoinRemoteSigner.parsePairing(this.pairing)
          saved = {...data, secret: Array.from(NostrTools.generateSecretKey())}
        } else {
          saved = JSON.parse(localStorage.getItem(this.storageKey) || 'null')
          if (!saved) throw new Error('Pair this browser first')
          BitcoinRemoteSigner.parsePairing(
            JSON.stringify({...saved, token: '0'.repeat(32)})
          )
        }
        if (
          !Array.isArray(saved.secret) ||
          saved.secret.length !== 32 ||
          saved.secret.some(n => !Number.isInteger(n) || n < 0 || n > 255)
        )
          throw new Error('Invalid saved browser identity')
        this.client = Vue.markRaw(
          new BitcoinRemoteSigner.NostrBitcoinSigner({
            network: this.network,
            ...saved,
            secret: new Uint8Array(saved.secret),
            onStatus: status => {
              this.signingError = false
              this.message = status
              this.pinRequired = status === 'PIN required'
            }
          })
        )
        this.client.connect()
        if (pair) {
          if (!/^[\x20-\x7e]{1,40}$/.test(this.label))
            throw new Error('Use a client name of 1–40 plain text characters')
          this.message = `Approve ${this.label} on the device. Browser key: ${this.client.clientKey}`
          await this.client.request('pair', {
            token: saved.token,
            label: this.label
          })
          delete saved.token
          localStorage.setItem(this.storageKey, JSON.stringify(saved))
          this.refreshPairing()
          this.scan = false
          this.pairing = ''
        }
        this.message = pair
          ? 'Pairing approved. Retrieving public wallet…'
          : 'Connected. Retrieving public wallet…'
        this.account = await this.client.getAccount()
        this.connected = true
        this.$emit('device:connected', 'nostr-device')
        if (!forSigning) {
          await this.importAccount()
          if (this.imported) this.dialog = false
        }
      } catch (error) {
        this.message = error.message
        if (!this.stopping) this.disconnect()
      } finally {
        this.busy = false
      }
    },
    walletDescriptor(descriptor) {
      return descriptor
        .replace(
          /\/<([0-9]+(?:;[0-9]+)+)>\//g,
          (_match, branches) => '/{' + branches.replaceAll(';', ',') + '}/'
        )
        .split('#')[0]
    },
    async importAccount() {
      this.busy = true
      this.message = 'Importing public wallet…'
      try {
        const {data: wallet} = await LNbits.api.request(
          'GET',
          '/api/v1/wallet/onchain',
          this.adminkey
        )
        if (
          wallet?.onchain_network === this.network &&
          this.walletDescriptor(wallet?.onchain_meta?.masterpub || '') ===
            this.walletDescriptor(this.account.descriptor)
        ) {
          this.imported = true
          this.dialog = false
          this.message =
            'Connected. Your public wallet is already imported and ready to use.'
          this.$emit('wallet-imported')
          this.$q.notify({type: 'positive', message: this.message})
          return
        }
        if (wallet?.onchain_wallet_kind)
          throw new Error(
            'This LNbits wallet already has an account. Use a new wallet on the selected network to import the signer.'
          )
        await LNbits.api.request(
          'POST',
          '/api/v1/wallet/onchain',
          this.adminkey,
          {
            title: 'Remote Bitcoin signer',
            network: this.network,
            masterpub: this.walletDescriptor(this.account.descriptor),
            meta: {
              signer: 'nostr',
              accountPath: this.account.path
            }
          }
        )
        this.imported = true
        this.dialog = false
        this.$emit('wallet-imported')
        this.message =
          'Signer connected and public wallet imported. You can now receive coins on the selected network.'
        this.$q.notify({type: 'positive', message: this.message})
      } catch (error) {
        const detail = error.response?.data?.detail
        this.message =
          'Signer connected, but public wallet import failed. ' +
          (typeof detail === 'string'
            ? detail
            : error.message || 'Please retry the import.')
        this.$q.notify({type: 'warning', message: this.message})
      } finally {
        this.busy = false
      }
    },
    scanError(error) {
      this.scan = false
      this.message = error.message
    },
    detected(codes) {
      if (codes[0]?.rawValue) {
        this.pairing = codes[0].rawValue
        this.scan = false
      }
    }
  },
  mounted() {
    if (typeof localStorage !== 'undefined') this.refreshPairing()
    this.onVisibilityChange = () => {
      if (!document.hidden) return
      this.pin = ''
      // Approval can take place while this tab is hidden. Keep the request-bound
      // session alive until its result or timeout; unmount still disconnects.
      if (!this.signing) this.disconnect()
    }
    document.addEventListener('visibilitychange', this.onVisibilityChange)
  },
  beforeUnmount() {
    if (this.successCloseTimer !== null) {
      clearTimeout(this.successCloseTimer)
      this.successCloseTimer = null
    }
    document.removeEventListener('visibilitychange', this.onVisibilityChange)
    this.disconnect()
  }
})
