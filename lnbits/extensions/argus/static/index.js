window.app.component('argus-settings', {
  template: '#argus-settings',
  data() { return { isSuperUser: true,
      argus: null,
      argusForm: null,
      argusPolicy: {automatic: false, transaction: 0, hour: 0, day: 0},
      argusRequests: [],
      argusTimer: null,
      argusError: '',
  } },
  mounted() { this.loadArgus(); this.argusTimer = setInterval(() => this.loadArgus(), 2000) },
  beforeUnmount() { clearInterval(this.argusTimer) },
  methods: {
    async loadArgus() {
      try {
        const {data} = await LNbits.api.request('GET', '/api/v1/argus/settings')
        this.argusError = ''
        this.argus = data
        if (!this.argusForm) {
          this.argusForm = {
            enabled: data.enabled,
            timeout: data.timeout
          }
          this.argusPolicy = {
            automatic: data.policy.automatic,
            transaction: data.policy.transaction / 1000,
            hour: data.policy.hour / 1000,
            day: data.policy.day / 1000
          }
        }
        this.argusRequests = (
          await LNbits.api.request('GET', '/api/v1/argus/requests')
        ).data
      } catch (error) {
        this.argusError = 'Could not load Argus settings. Try again.'
      }
    },
    async argusAction(method, path, body) {
      try {
        await LNbits.api.request(method, '/api/v1/argus' + path, null, body)
        await this.loadArgus()
      } catch (error) {
        LNbits.utils.notifyApiError(error)
      }
    },
    async saveArgus() {
      if (this.argus.enabled && !this.argusForm.enabled) {
        this.$q
          .dialog({
            title: 'Disable Argus authorization?',
            message:
              'Queued payments will be cancelled. New payments will no longer require Argus.',
            cancel: true
          })
          .onOk(() => this.argusAction('PUT', '/settings', this.argusForm))
      } else await this.argusAction('PUT', '/settings', this.argusForm)
    },
    revokeArgus() {
      this.$q
        .dialog({
          title: 'Revoke Argus pairing?',
          message:
            'Queued payments will be cancelled. Authorization remains enabled until you explicitly disable it or pair a device.',
          cancel: true
        })
        .onOk(() => this.argusAction('DELETE', '/pair'))
    }
  }
})
