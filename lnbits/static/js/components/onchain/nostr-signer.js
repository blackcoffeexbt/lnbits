window.app.component('onchain-nostr-signer', {
  template: '#onchain-nostr-signer',
  props: ['network', 'user-id', 'wallet-id', 'adminkey'],
  emits: ['device:connected', 'wallet-imported'],
  data() {
    return {
      dialog: false,
      signingDialog: false,
      pairing: '',
      label: 'LNbits browser',
      client: null,
      connected: false,
      imported: false,
      busy: false,
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
    dialogOpen: {
      get() {
        return this.dialog || this.signingDialog
      },
      set(value) {
        this.dialog = value
        if (!value) this.signingDialog = false
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
    }
  },
  methods: {
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
    async signPsbt(psbt) {
      if (this.signing) throw new Error('A signing request is already active')
      this.signing = true
      this.dialog = false
      this.signingDialog = true
      this.message = 'Connecting to signer…'
      try {
        await this.ensureConnected()
        this.message = 'Waiting for device…'
        const signed = await this.client.sign(psbt)
        this.message = 'Signing complete'
        return signed
      } catch (error) {
        this.message = error.message
        throw error
      } finally {
        this.signingDialog = false
        this.signing = false
        this.pinRequired = false
        this.pin = ''
      }
    },
    async submitPin() {
      this.pinBusy = true
      this.pinRequired = false
      let pin = this.pin
      this.pin = ''
      try {
        const submission = this.client.submitPin(pin)
        pin = ''
        await submission
      } catch (error) {
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
          this.pairing = ''
        }
        this.message = pair
          ? 'Pairing approved. Retrieving public wallet…'
          : 'Connected. Retrieving public wallet…'
        this.account = await this.client.getAccount()
        this.connected = true
        this.$emit('device:connected', 'nostr-device')
        if (!forSigning) await this.importAccount()
      } catch (error) {
        this.message = error.message
        this.disconnect()
      } finally {
        this.busy = false
      }
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
          wallet?.onchain_meta?.masterpub === this.account.descriptor
        ) {
          this.imported = true
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
            masterpub: this.account.descriptor,
            meta: {
              signer: 'nostr',
              accountPath: this.account.path
            }
          }
        )
        this.imported = true
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
    detected(codes) {
      if (codes[0]?.rawValue) {
        this.pairing = codes[0].rawValue
        this.scan = false
      }
    }
  },
  mounted() {
    this.onVisibilityChange = () => {
      if (document.hidden) this.disconnect()
    }
    document.addEventListener('visibilitychange', this.onVisibilityChange)
  },
  beforeUnmount() {
    document.removeEventListener('visibilitychange', this.onVisibilityChange)
    this.disconnect()
  }
})
