<template id="onchain-nostr-signer">
  <div>
    <q-btn
      split
      unelevated
      color="primary"
      label="Pair Remote Signer"
      @click="dialog = true"
    ></q-btn>
    <q-dialog v-model="dialogOpen" :persistent="signing">
      <q-card style="width: 560px; max-width: 95vw">
        <q-card-section
          ><div class="text-h6">Pair a Remote Signer</div>
          <p v-if="!signingDialog">
            Scan the pairing code on the remote signer using the "Scan Pairing
            Code" button. Change the Client name if needed, this is shown on the
            signing device during pairing.
          </p>
          <div v-if="!signingDialog">
            <q-input
              v-model="label"
              label="Client name"
              maxlength="40"
              :disable="busy"
            ></q-input>
            <q-btn
              flat
              label="Scan Pairing Code"
              @click="scan = !scan"
              :disable="busy"
            ></q-btn>
            <qrcode-stream
              v-if="scan"
              @detect="detected"
              @error="message = $event.message"
            ></qrcode-stream>
            <q-input
              v-model="pairing"
              type="text"
              readonly
              label="Pairing code"
              :disable="busy"
            ></q-input>
          </div>
          <q-form v-if="pinRequired" @submit="submitPin">
            <q-input
              v-model="pin"
              type="password"
              label="Device PIN"
              inputmode="numeric"
              autocomplete="off"
              maxlength="32"
              autofocus
              :disable="pinBusy"
              hint="Sent encrypted directly to your paired device"
              :rules="[
                value => /^[0-9]{6,32}$/.test(value) || 'Use 6–32 digits'
              ]"
            />
            <q-btn
              type="submit"
              label="Unlock for signing"
              color="primary"
              :loading="pinBusy"
            />
          </q-form>
          <p
            class="q-mt-md"
            style="overflow-wrap: anywhere"
            role="status"
            aria-live="polite"
          >
            {{ message }}
          </p>
        </q-card-section>
        <q-card-actions v-if="!signingDialog" align="left">
          <q-btn
            label="Pair"
            color="primary"
            :loading="busy"
            :disable="!pairing"
            @click="connect(true)"
          ></q-btn>
          <q-btn
            label="Reconnect"
            flat
            :disable="busy"
            @click="connect(false)"
          ></q-btn>
          <q-btn
            v-if="connected && !imported"
            label="Retry public wallet import"
            flat
            :disable="busy"
            @click="importAccount()"
          ></q-btn>
          <q-btn
            label="Forget pairing"
            flat
            :disable="busy"
            @click="forget"
          ></q-btn>
          <q-btn label="Close" flat v-close-popup></q-btn>
        </q-card-actions>
      </q-card>
    </q-dialog>
  </div>
</template>
