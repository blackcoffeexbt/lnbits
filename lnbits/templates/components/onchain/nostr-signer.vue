<template id="onchain-nostr-signer">
  <div>
    <div v-if="paired" class="row items-center q-gutter-sm">
      <q-icon name="verified_user" color="positive" size="24px"></q-icon>
      <div>
        <div class="text-weight-medium">Paired with remote signer</div>
        <div class="text-caption text-grey">
          Connects automatically when you sign
        </div>
      </div>
      <q-btn
        flat
        no-caps
        color="negative"
        label="Remove pairing"
        :disable="signing || busy"
        @click="forget"
      ></q-btn>
    </div>
    <q-btn
      v-else
      unelevated
      no-caps
      color="primary"
      icon="qr_code_scanner"
      label="Pair remote signer"
      @click="dialog = true"
    ></q-btn>
    <q-dialog v-model="dialogOpen" :persistent="signing || busy || stopping">
      <q-card class="remote-signer-card" style="width: 480px; max-width: 95vw">
        <q-card-section class="q-pa-lg">
          <div class="row items-center no-wrap q-mb-lg">
            <q-icon
              name="security"
              color="primary"
              size="28px"
              class="q-mr-sm"
            ></q-icon>
            <div
              class="text-h6"
              v-text="
                signingDialog ? 'Remote Bitcoin Signer' : 'Pair a remote signer'
              "
            ></div>
          </div>
          <div v-if="!signingDialog" class="q-gutter-y-md">
            <p class="text-body2">
              Open pairing on your signer, then scan its QR code. Approve this
              browser on the device to finish.
            </p>
            <q-input
              outlined
              v-model="label"
              label="Client name"
              hint="Shown on your signer during pairing"
              maxlength="40"
              :disable="busy"
            ></q-input>
            <template v-if="!paired">
              <q-btn
                class="full-width q-py-sm"
                unelevated
                no-caps
                color="primary"
                :icon="scan ? 'close' : 'qr_code_scanner'"
                :label="scan ? 'Cancel scanning' : 'Scan pairing code'"
                @click="scan = !scan"
                :disable="busy"
              ></q-btn>
              <qrcode-stream
                v-if="scan"
                @detect="detected"
                @error="scanError"
              ></qrcode-stream>
              <q-input
                outlined
                autogrow
                v-model="pairing"
                type="textarea"
                label="Or paste pairing code"
                :disable="busy"
              ></q-input>
            </template>
            <q-banner v-else rounded class="bg-transparent"
              >This browser is paired. Reconnect to finish importing your public
              wallet.</q-banner
            >
            <p
              v-if="message"
              class="text-body2"
              style="overflow-wrap: anywhere"
              role="status"
              aria-live="polite"
              v-text="message"
            ></p>
          </div>
          <div v-else>
            <div
              class="remote-signer-progress"
              :class="{
                'text-positive': signingComplete,
                'text-warning': progress.icon === 'warning',
                'text-primary': !signingComplete && progress.icon !== 'warning'
              }"
              role="img"
              :aria-label="progress.title"
            >
              <svg viewBox="0 0 240 240" aria-hidden="true">
                <path
                  v-for="(arc, i) in [
                    'M 114 22 A 98 98 0 0 0 22 114',
                    'M 126 22 A 98 98 0 0 1 218 114',
                    'M 218 126 A 98 98 0 0 1 126 218',
                    'M 114 218 A 98 98 0 0 1 22 126'
                  ]"
                  :key="i"
                  :d="arc"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="12"
                  stroke-linecap="round"
                  :opacity="i < progress.step ? 1 : 0.15"
                ></path>
                <path
                  v-if="
                    signing && !signingError && message === 'Decrypting wallet'
                  "
                  class="remote-signer-decrypt"
                  d="M 126 22 A 98 98 0 0 1 218 114"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="12"
                  stroke-linecap="round"
                  pathLength="100"
                ></path>
              </svg>
              <q-icon
                :key="progress.icon"
                :name="progress.icon"
                size="64px"
                :class="{'remote-signer-tick': signingComplete}"
              ></q-icon>
              <q-spinner
                v-if="signing && !pinRequired && progress.icon !== 'warning'"
                class="remote-signer-spinner"
                size="22px"
              ></q-spinner>
            </div>
            <div class="text-center q-mb-lg" role="status" aria-live="polite">
              <div class="text-h6 q-mb-xs" v-text="progress.title"></div>
              <div
                class="text-body2"
                style="overflow-wrap: anywhere"
                v-text="progress.detail"
              ></div>
            </div>
            <q-form
              v-if="pinRequired"
              @submit="submitPin"
              class="q-gutter-y-md"
            >
              <q-input
                outlined
                v-model="pin"
                type="password"
                label="Wallet Decryption PIN"
                inputmode="numeric"
                autocomplete="off"
                maxlength="32"
                autofocus
                :disable="pinBusy"
                hint="Sent encrypted directly to your paired device"
                :rules="[
                  value => /^[0-9]{6,32}$/.test(value) || 'Use 6–32 digits'
                ]"
              ></q-input>
              <q-btn
                class="full-width q-py-sm"
                unelevated
                no-caps
                icon="lock_open"
                type="submit"
                label="Unlock for signing"
                color="primary"
                :loading="pinBusy"
                :disable="!/^[0-9]{6,32}$/.test(pin)"
              ></q-btn>
            </q-form>
          </div>
        </q-card-section>
        <q-card-actions
          v-if="
            (signing || busy || stopping) &&
            !signerResultReceived &&
            !signingComplete
          "
          align="right"
          class="q-px-lg q-pb-lg"
        >
          <q-btn
            flat
            no-caps
            label="Stop waiting"
            :loading="stopping"
            @click="stopWaiting"
          ></q-btn>
        </q-card-actions>
        <q-card-actions
          v-if="signingDialog && !signing && !signingComplete"
          align="right"
          class="q-px-lg q-pb-lg"
        >
          <q-btn label="Close" no-caps unelevated v-close-popup></q-btn>
        </q-card-actions>
        <q-card-actions
          v-if="!signingDialog"
          align="right"
          class="q-px-lg q-pb-lg"
        >
          <q-btn
            label="Cancel"
            no-caps
            flat
            :disable="busy"
            v-close-popup
          ></q-btn>
          <q-btn
            v-if="connected && !imported"
            label="Retry wallet import"
            no-caps
            unelevated
            color="primary"
            :loading="busy"
            @click="importAccount()"
          ></q-btn>
          <q-btn
            v-else
            :label="paired ? 'Reconnect' : 'Pair signer'"
            no-caps
            unelevated
            color="primary"
            :loading="busy"
            :disable="!paired && !pairing.trim()"
            @click="connect(!paired)"
          ></q-btn>
        </q-card-actions>
      </q-card>
    </q-dialog>
  </div>
</template>
